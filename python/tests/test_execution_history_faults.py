"""History maintenance publishes complete private artifacts and never changes its source authority."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from exacly import completion_store, planning
from exacly import execution_history as history
from exacly import execution_maintenance as copying
from exacly import execution_store as store
from exacly import history_maintenance as maintenance
from ledger_history_fixtures import build
from test_execution_maintenance import private_tree, snapshot

Doc = dict[str, Any]


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    root = tmp_path / "history"
    root.mkdir(mode=0o700)
    return build(root, 2, depth=2).ledger


@pytest.fixture
def output(tmp_path: Path) -> Path:
    parent = tmp_path / "exports"
    parent.mkdir(mode=0o700)
    return parent / "index"


def valid_report(report: Doc, output: Path | None = None) -> None:
    jsonschema.validate(report, maintenance.SCHEMA)
    assert report["digest"] == planning.digest({key: value for key, value in report.items() if key != "digest"})
    assert report["profile"] == history.profile()
    assert report["claims"] and all(value is False for value in report["claims"].values())
    assert report["validation"]["semantics"]["reused"] == 0
    if output is not None:
        for item in report["files"]:
            content = (output / item["path"]).read_bytes()
            assert item["digest"] == "sha256:" + hashlib.sha256(content).hexdigest()
            assert item["size"] == len(content)


def retained(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Read the original tables directly: conversion must preserve every TEXT/BLOB value exactly."""
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "bindings",
                "active",
                "watermarks",
                "completion_intents",
                "completion_receipts",
            )
        }


def test_conversion_preserves_exact_source_and_all_retained_proof_then_audits_fresh(ledger: Path, output: Path) -> None:
    before = snapshot(ledger.parent)
    source_rows = retained(ledger)
    report = maintenance.convert(ledger, output)
    valid_report(report, output)
    assert report["operation"] == "index" and report["status"] == "indexed" and report["dry_run"] is False
    assert report["validation"]["integrity_baseline_checked"] is True
    assert report["validation"]["all_retained_bytes_checked"] is True
    assert report["validation"]["semantics"] == {"bindings": 4, "fresh": 2, "reused": 0}
    assert {item["path"] for item in report["files"]} == {"raw/ledger.sqlite3", "indexed/ledger.sqlite3"}
    assert report["observed_store"]["user_version"] == store.COMPLETION_VERSION
    assert (output / "raw/ledger.sqlite3").read_bytes() == before[ledger.name]
    indexed = output / "indexed/ledger.sqlite3"
    assert int.from_bytes(indexed.read_bytes()[60:64], "big") == history.VERSION
    assert retained(indexed) == source_rows
    assert snapshot(ledger.parent) == before
    assert json.loads((output / "manifest.json").read_bytes()) == report
    private_tree(output)
    indexed_before = snapshot(indexed.parent)
    audited = maintenance.audit(indexed)
    valid_report(audited)
    assert audited["status"] == "passed"
    assert audited["validation"] == report["validation"]
    assert snapshot(indexed.parent) == indexed_before
    assert snapshot(ledger.parent) == before
    assert "audit-only" in maintenance.as_text(audited)


def test_dry_run_fully_validates_without_creating_a_destination_or_temporary_files(ledger: Path, output: Path) -> None:
    before = snapshot(ledger.parent)
    exports = list(output.parent.iterdir())
    report = maintenance.convert(ledger, output, dry_run=True)
    valid_report(report)
    assert report["status"] == "ready" and report["dry_run"] is True
    assert report["output"] == str(output)
    assert report["files"] == []
    assert report["validation"]["status"] == "passed"
    assert report["validation"]["integrity_baseline_checked"] is False
    assert report["validation"]["semantics"] == {"bindings": 4, "fresh": 2, "reused": 0}
    assert snapshot(ledger.parent) == before and list(output.parent.iterdir()) == exports


def test_dry_run_and_conversion_share_the_unsupported_text_encoding_gate(tmp_path: Path, output: Path) -> None:
    private = tmp_path / "utf16-operator"
    private.mkdir(mode=0o700)
    ledger = private / "ledger.sqlite3"
    with closing(sqlite3.connect(ledger)) as connection:
        connection.execute("PRAGMA encoding='UTF-16le'")
        connection.execute(f"PRAGMA application_id={store.APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={store.COMPLETION_VERSION}")
        for statement in (*store.SCHEMA.values(), *completion_store.SCHEMA.values()):
            connection.execute(statement)
        connection.commit()
    ledger.chmod(0o600)
    # This is a valid legacy empty v2 ledger; only the new history codec excludes its text encoding.
    assert copying.inspect(ledger)["status"] == "clean"
    before = snapshot(private)
    diagnostics = []
    for dry_run in (True, False):
        with pytest.raises(history.HistoryError, match="UTF-8 SQLite text encoding") as raised:
            maintenance.convert(ledger, output, dry_run=dry_run)
        diagnostics.append(str(raised.value))
        assert snapshot(private) == before
        assert not output.exists() and list(output.parent.iterdir()) == []
    assert diagnostics[0] == diagnostics[1]


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_v2_proof_is_never_blessed_even_by_a_dry_run(ledger: Path, output: Path, dry_run: bool) -> None:
    with sqlite3.connect(ledger) as connection:
        connection.execute("UPDATE completion_intents SET report=?", (b"invented passing report",))
    before = snapshot(ledger.parent)
    with pytest.raises(store.StoreError):
        maintenance.convert(ledger, output, dry_run=dry_run)
    assert snapshot(ledger.parent) == before
    assert not output.exists() and list(output.parent.iterdir()) == []


def test_unresolved_prepared_completion_requires_explicit_recovery_before_indexing(
    tmp_path: Path, output: Path
) -> None:
    from test_completion_faults import kill_at, prepare_case

    case = prepare_case(tmp_path)
    kill_at(case, "after_prepare")
    before = snapshot(case.ledger.parent)
    for dry_run in (True, False):
        with pytest.raises(store.StoreError, match=r"prepared|recovery"):
            maintenance.convert(case.ledger, output, dry_run=dry_run)
        assert snapshot(case.ledger.parent) == before
        assert not output.exists() and list(output.parent.iterdir()) == []


def test_full_audit_reports_corruption_without_repairing_or_resealing(ledger: Path, output: Path) -> None:
    maintenance.convert(ledger, output)
    indexed = output / "indexed/ledger.sqlite3"
    with sqlite3.connect(indexed) as connection:
        connection.execute("UPDATE completion_intents SET report=?", (b"changed retained bytes",))
    before = snapshot(indexed.parent)
    report = maintenance.audit(indexed)
    valid_report(report)
    assert report["status"] == "corrupt" and report["validation"]["status"] == "failed"
    assert report["diagnostics"]
    assert snapshot(indexed.parent) == before
    assert maintenance.audit(indexed)["status"] == "corrupt"
    assert snapshot(indexed.parent) == before


def test_unsupported_source_is_classified_without_opening_sqlite(ledger: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = snapshot(ledger.parent)
    connect = sqlite3.connect

    def fail_connect(*args: Any, **kwargs: Any) -> Any:
        if args and args[0] == ":memory:":
            return connect(*args, **kwargs)
        raise AssertionError("unsupported history audit must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    report = maintenance.audit(ledger)
    valid_report(report)
    assert report["status"] == "unsupported" and report["validation"]["status"] == "not_performed"
    assert snapshot(ledger.parent) == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_audit_never_opens_sqlite_with_unsupported_sidecars(
    ledger: Path, output: Path, suffix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    maintenance.convert(ledger, output)
    indexed = output / "indexed/ledger.sqlite3"
    sidecar = Path(str(indexed) + suffix)
    sidecar.write_bytes(b"unsupported residue preserved for the operator")
    sidecar.chmod(0o600)
    before = snapshot(indexed.parent)
    connect = sqlite3.connect

    def fail_connect(*args: Any, **kwargs: Any) -> Any:
        if args and args[0] == ":memory:":
            return connect(*args, **kwargs)
        raise AssertionError("audit must classify residue before opening SQLite")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    report = maintenance.audit(indexed)
    valid_report(report)
    assert report["status"] == "unsupported"
    assert report["validation"]["status"] == "not_performed"
    assert snapshot(indexed.parent) == before


def test_indexing_rejects_v1_and_already_indexed_sources_without_writing(
    ledger: Path, output: Path, tmp_path: Path
) -> None:
    from test_completion import prepare_scenario

    root = tmp_path / "v1"
    root.mkdir(mode=0o700)
    original = prepare_scenario(root, legacy_ledger=True).ledger
    original_before = snapshot(original.parent)
    for dry_run in (False, True):
        with pytest.raises(store.StoreError, match="version 2"):
            maintenance.convert(original, output, dry_run=dry_run)
        assert snapshot(original.parent) == original_before
        assert not output.exists()
    maintenance.convert(ledger, output)
    indexed = output / "indexed/ledger.sqlite3"
    indexed_before = snapshot(indexed.parent)
    again = output.parent / "again"
    for dry_run in (False, True):
        with pytest.raises(store.StoreError):
            maintenance.convert(indexed, again, dry_run=dry_run)
        assert snapshot(indexed.parent) == indexed_before
        assert not again.exists()


def test_index_initialization_failure_discards_only_the_private_working_copy(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(ledger.parent)
    original = history.initialize

    def fail_after_initialization(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
        original(connection, deadline=deadline)
        assert connection.in_transaction
        raise store.StoreError("injected failure after history initialization")

    monkeypatch.setattr(history, "initialize", fail_after_initialization)
    with pytest.raises(store.StoreError, match="injected failure"):
        maintenance.convert(ledger, output)
    assert snapshot(ledger.parent) == before
    assert not output.exists() and list(output.parent.iterdir()) == []


def test_exact_output_is_audited_before_publication(
    ledger: Path, output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(ledger.parent)

    def reject_copy(connection: sqlite3.Connection, *, deadline: store.Deadline | None = None) -> Doc:
        assert connection.execute("PRAGMA user_version").fetchone() == (history.VERSION,)
        raise store.StoreError("injected exact output audit failure")

    monkeypatch.setattr(history, "validate", reject_copy)
    with pytest.raises(store.StoreError, match="exact output audit"):
        maintenance.convert(ledger, output)
    assert snapshot(ledger.parent) == before
    assert not output.exists() and list(output.parent.iterdir()) == []


@pytest.mark.parametrize("phase", ["before_publication", "after_publication"])
def test_fsync_failure_preserves_the_source_and_any_complete_published_artifact(
    ledger: Path, output: Path, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(ledger.parent)
    fsync = os.fsync

    def fail_after_publication(fd: int) -> None:
        if output.exists():
            raise OSError("injected parent fsync failure")
        fsync(fd)

    def fail_before_publication(directory: Path) -> None:
        assert (directory / "manifest.json").is_file()
        raise OSError("injected working copy fsync failure")

    if phase == "after_publication":
        monkeypatch.setattr(os, "fsync", fail_after_publication)
        message = "copy was published but durability could not be confirmed"
    else:
        monkeypatch.setattr(copying, "_sync", fail_before_publication)
        message = "source files were preserved"
    with pytest.raises(store.StoreError, match=message):
        maintenance.convert(ledger, output)
    assert snapshot(ledger.parent) == before
    monkeypatch.undo()
    if phase == "after_publication":
        manifest = json.loads((output / "manifest.json").read_bytes())
        valid_report(manifest, output)
        assert maintenance.audit(output / "indexed/ledger.sqlite3")["status"] == "passed"
        private_tree(output)
    else:
        assert not output.exists() and list(output.parent.iterdir()) == []


def paused_conversion(path: Path, out: Path, phase: str, control: Connection) -> None:
    """Pause at real initialization or publication boundaries; the parent sends SIGKILL."""
    connect, publish, rename = sqlite3.connect, copying._publish, os.rename

    def pause() -> None:
        control.send("paused")
        control.recv()

    class PausedCommit(sqlite3.Connection):
        def commit(self) -> None:
            if self.execute("PRAGMA user_version").fetchone() == (history.VERSION,) and self.in_transaction:
                pause()
            super().commit()

    def paused_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return connect(*args, **kwargs, factory=PausedCommit)

    def before_publish(folder: Path, destination: Path) -> None:
        assert (folder / "manifest.json").is_file()
        pause()
        publish(folder, destination)

    def after_rename(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if Path(destination) == out and phase == "reserved_before_rename":
            assert out.is_dir() and list(out.iterdir()) == []
            pause()
        rename(source, destination, *args, **kwargs)
        if Path(destination) == out:
            pause()

    try:
        with pytest.MonkeyPatch.context() as patched:
            if phase == "uncommitted_index":
                patched.setattr(sqlite3, "connect", paused_connect)
            elif phase == "before_publication":
                patched.setattr(copying, "_publish", before_publish)
            else:
                # Both modules hold the same os module; this targets _publish's real rename call.
                patched.setattr(os, "rename", after_rename)
            control.send(("unexpected_result", maintenance.convert(path, out)))
    except BaseException as error:
        control.send(("error", repr(error)))
        raise
    finally:
        control.close()


@contextmanager
def conversion_at(ledger: Path, output: Path, phase: str) -> Iterator[BaseProcess]:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=paused_conversion, args=(ledger, output, phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20), "history conversion did not reach the selected durability boundary"
        assert parent.recv() == "paused"
        yield process
    finally:
        if process.is_alive():
            assert process.pid is not None
            os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive() and process.exitcode == -signal.SIGKILL
        parent.close()


@pytest.mark.parametrize(
    "phase", ["uncommitted_index", "before_publication", "reserved_before_rename", "after_publication"]
)
def test_sigkill_publishes_no_partial_artifact_and_releases_all_locks(ledger: Path, output: Path, phase: str) -> None:
    before = snapshot(ledger.parent)
    with conversion_at(ledger, output, phase):
        with pytest.raises(store.LockTimeoutError, match="timed out"):
            maintenance.convert(ledger, output.parent / "other", lock_timeout=0)
        assert snapshot(ledger.parent) == before
    assert snapshot(ledger.parent) == before
    if phase == "after_publication":
        report = json.loads((output / "manifest.json").read_bytes())
        valid_report(report, output)
        assert maintenance.audit(output / "indexed/ledger.sqlite3", lock_timeout=0)["status"] == "passed"
        with pytest.raises(store.StoreError, match="new directory"):
            maintenance.convert(ledger, output, lock_timeout=0)
        assert json.loads((output / "manifest.json").read_bytes()) == report
    elif phase == "reserved_before_rename":
        assert output.is_dir() and list(output.iterdir()) == []
        assert not (output / "manifest.json").exists()
        with pytest.raises(store.StoreError):
            maintenance.audit(output / "indexed/ledger.sqlite3", lock_timeout=0)
        with pytest.raises(store.StoreError, match="new directory"):
            maintenance.convert(ledger, output, lock_timeout=0)
        fresh = output.parent / "fresh-destination"
        assert maintenance.convert(ledger, fresh, lock_timeout=0)["status"] == "indexed"
        assert maintenance.audit(fresh / "indexed/ledger.sqlite3")["status"] == "passed"
        assert list(output.iterdir()) == []
        private_tree(fresh)
    else:
        assert not output.exists()
        # SIGKILL may retain a private staging directory; it never becomes the named artifact.
        for abandoned in output.parent.iterdir():
            private_tree(abandoned)
        assert maintenance.convert(ledger, output, lock_timeout=0)["status"] == "indexed"
    assert snapshot(ledger.parent) == before
    private_tree(output)


def test_output_cannot_overwrite_existing_operator_content(ledger: Path, output: Path) -> None:
    output.mkdir(mode=0o700)
    marker = output / "keep.txt"
    marker.write_bytes(b"existing operator content")
    marker.chmod(0o600)
    before = snapshot(ledger.parent)
    for dry_run in (True, False):
        with pytest.raises(store.StoreError, match="new directory"):
            maintenance.convert(ledger, output, dry_run=dry_run)
        assert marker.read_bytes() == b"existing operator content"
        assert snapshot(ledger.parent) == before


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_dry_run_cannot_reserve_a_source_or_sqlite_sidecar_name(ledger: Path, suffix: str) -> None:
    before = snapshot(ledger.parent)
    with pytest.raises(store.StoreError, match="overlaps"):
        maintenance.convert(ledger, Path(str(ledger) + suffix), dry_run=True)
    assert snapshot(ledger.parent) == before

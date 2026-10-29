"""Operational reuse retains mandatory exact-byte checks and fresh global authority."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest
from exacly import completion, completion_store, planning, state_writer
from exacly import execution_history as history
from exacly import execution_index as index
from exacly import execution_store as store
from test_admission import CASE, Source
from test_completion import prepare_scenario
from test_execution_history import mutate, source_columns, source_rows
from test_execution_history import v2 as v2_fixture

Doc = dict[str, Any]
v2 = v2_fixture


@contextmanager
def connection(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, isolation_level=None)) as database:
        if write:
            database.execute("PRAGMA synchronous=EXTRA")
            database.execute("PRAGMA foreign_keys=ON")
        database.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield database
        finally:
            if database.in_transaction:
                database.rollback()


def initialize(path: Path) -> Doc:
    with connection(path, write=True) as database:
        result = index.initialize(database)
        database.commit()
        return result


@pytest.fixture
def ledger(tmp_path: Path, v2: Path) -> Path:
    private = tmp_path / "indexed-operator"
    private.mkdir(mode=0o700)
    path = private / "ledger.sqlite3"
    shutil.copyfile(v2, path)
    path.chmod(0o600)
    initialize(path)
    return path


def check(path: Path) -> index.Checked:
    with connection(path) as database:
        return index.check(database)


def audit(path: Path) -> Doc:
    with connection(path) as database:
        return index.audit(database)


def stale_profile(path: Path, *, unknown: bool = False) -> None:
    with connection(path) as database:
        profile = json.loads(database.execute("SELECT profile_json FROM history_profile").fetchone()[0])
    profile.pop("digest")
    if unknown:
        profile["family"] = "unknown-future-family"
    else:
        profile["inputs"][0]["digest"] = "sha256:" + "0" * 64
    profile["digest"] = planning.digest(profile)
    mutate(path, "UPDATE history_profile SET profile_json=?", (store.canonical(profile),))
    mutate(path, "UPDATE history_semantics SET profile_digest=?", (profile["digest"],))


def test_matching_terminal_profiles_reuse_semantics_while_preserving_all_bytes(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = ledger.read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("matching terminal proof was semantically reinterpreted")

    monkeypatch.setattr(completion_store, "_payload", forbidden)
    checked = check(ledger)
    assert checked.facts["semantics"] == {"bindings": 1, "fresh": 0, "reused": 2, "prepared": 0}
    assert checked.facts["integrity"]["complete"] is True
    assert checked.facts["index_rows"] == {"integrity": 6, "semantic": 2}
    assert checked.current_profile is True
    assert ledger.read_bytes() == before


def test_full_audit_ignores_matching_positive_verdicts(ledger: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = completion_store._payload

    def counted(envelope: Doc, binding: Doc) -> tuple[Doc, Doc, Doc]:
        calls.append(envelope["phase"])
        return original(envelope, binding)

    monkeypatch.setattr(completion_store, "_payload", counted)
    before = ledger.read_bytes()
    facts = audit(ledger)
    assert sorted(calls) == ["aborted", "committed"]
    assert facts["semantics"] == {"bindings": 1, "fresh": 2, "reused": 0, "prepared": 0}
    assert ledger.read_bytes() == before


def test_missing_optional_verdict_gets_read_only_fresh_fallback(ledger: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mutate(
        ledger,
        "DELETE FROM history_semantics WHERE intent_id=(SELECT id FROM completion_intents WHERE phase='aborted')",
    )
    calls: list[str] = []
    original = completion_store._payload

    def counted(envelope: Doc, binding: Doc) -> tuple[Doc, Doc, Doc]:
        calls.append(envelope["phase"])
        return original(envelope, binding)

    monkeypatch.setattr(completion_store, "_payload", counted)
    before = ledger.read_bytes()
    facts = check(ledger).facts
    assert calls == ["aborted"]
    assert facts["semantics"] == {"bindings": 1, "fresh": 1, "reused": 1, "prepared": 0}
    assert facts["index_rows"]["semantic"] == 1
    assert ledger.read_bytes() == before


def test_known_stale_profile_requires_fresh_read_only_fallback_and_explicit_copy_refresh(ledger: Path) -> None:
    original = source_rows(ledger)
    stale_profile(ledger)
    before = ledger.read_bytes()
    checked = check(ledger)
    assert checked.current_profile is False
    assert checked.facts["profile_status"] == "known_stale"
    assert checked.facts["semantics"]["fresh"] == 2
    assert checked.facts["semantics"]["reused"] == 0
    with pytest.raises(history.HistoryError, match="activate refresh"):
        index.require_writable(checked)
    assert ledger.read_bytes() == before
    assert audit(ledger)["semantics"]["reused"] == 0
    refreshed = initialize(ledger)
    assert refreshed["profile_status"] == "current"
    assert source_rows(ledger) == original
    assert check(ledger).facts["semantics"]["reused"] == 2


def test_unknown_profile_is_unsupported_but_never_masks_baseline_corruption(ledger: Path) -> None:
    stale_profile(ledger, unknown=True)
    with pytest.raises(index.UnsupportedProfileError):
        check(ledger)
    mutate(ledger, "UPDATE history_integrity SET row_digest=?", ("sha256:" + "0" * 64,))
    with pytest.raises(history.HistoryError, match="baseline") as raised:
        audit(ledger)
    assert not isinstance(raised.value, index.UnsupportedProfileError)
    with pytest.raises(history.HistoryError, match="baseline"):
        initialize(ledger)


def test_unknown_profile_version_is_explicitly_unsupported(ledger: Path) -> None:
    with connection(ledger) as database:
        profile = json.loads(database.execute("SELECT profile_json FROM history_profile").fetchone()[0])
    profile.pop("digest")
    profile["format_version"] = "999.0.0"
    profile["digest"] = planning.digest(profile)
    mutate(ledger, "UPDATE history_profile SET profile_json=?", (store.canonical(profile),))
    mutate(ledger, "UPDATE history_semantics SET profile_digest=?", (profile["digest"],))
    with pytest.raises(index.UnsupportedProfileError):
        check(ledger)


@pytest.mark.parametrize("table,column", source_columns())
def test_reuse_checks_every_actual_sql_source_cell_before_semantics(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, table: str, column: str
) -> None:
    if column == "phase":
        value: Any = "aborted"
        condition = "phase='committed'"
    elif column == "serial":
        value, condition = 999, "1"
    else:
        value, condition = b"changed retained bytes", "1"
    mutate(
        ledger,
        f"UPDATE {table} SET {column}=? WHERE rowid=(SELECT rowid FROM {table} WHERE {condition} LIMIT 1)",
        (value,),
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid baseline reached proof semantics")

    monkeypatch.setattr(completion_store, "_payload", forbidden)
    before = ledger.read_bytes()
    with pytest.raises(store.StoreError):
        check(ledger)
    assert ledger.read_bytes() == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("profile_digest", "sha256:" + "0" * 64),
        ("dependencies_digest", "sha256:" + "0" * 64),
        ("verdict_json", "{}"),
        ("verdict_digest", "sha256:" + "0" * 64),
    ],
)
def test_malformed_present_current_profile_verdict_is_a_hard_error(ledger: Path, column: str, value: str) -> None:
    mutate(ledger, f"UPDATE history_semantics SET {column}=?", (value,))
    before = ledger.read_bytes()
    with pytest.raises(history.HistoryError):
        check(ledger)
    with pytest.raises(history.HistoryError):
        initialize(ledger)
    assert ledger.read_bytes() == before


def test_full_audit_and_refresh_do_not_repair_inconsistent_same_profile_derived_facts(ledger: Path) -> None:
    with connection(ledger) as database:
        identity, text = database.execute("SELECT intent_id,verdict_json FROM history_semantics LIMIT 1").fetchone()
    value = json.loads(text)
    value["before_snapshot"]["serial"] += 1
    mutate(
        ledger,
        "UPDATE history_semantics SET verdict_json=?,verdict_digest=? WHERE intent_id=?",
        (store.canonical(value), planning.digest(value), identity),
    )
    before = ledger.read_bytes()
    with pytest.raises(history.HistoryError, match="differs from fresh"):
        audit(ledger)
    with pytest.raises(history.HistoryError, match="differs from fresh"):
        initialize(ledger)
    assert ledger.read_bytes() == before


def test_current_v3_audit_copy_converts_with_exact_proof_and_new_snapshot_facts(tmp_path: Path, v2: Path) -> None:
    path = tmp_path / "v3.sqlite3"
    shutil.copyfile(v2, path)
    with connection(path, write=True) as database:
        history.initialize(database)
        database.commit()
    original = source_rows(path)
    with connection(path) as database:
        source = index.source_facts(database)
    assert source["format_version"] == 3
    assert source["integrity_baseline_checked"] is True
    initialize(path)
    assert source_rows(path) == original
    assert check(path).facts["semantics"]["reused"] == 2


def test_older_known_v3_profile_converts_only_after_its_existing_baseline_passes(tmp_path: Path, v2: Path) -> None:
    path = tmp_path / "v3.sqlite3"
    shutil.copyfile(v2, path)
    with connection(path, write=True) as database:
        history.initialize(database)
        database.commit()
    with connection(path) as database:
        profile = json.loads(database.execute("SELECT profile_json FROM history_profile").fetchone()[0])
    profile.pop("digest")
    profile["inputs"][0]["digest"] = "sha256:" + "0" * 64
    profile["digest"] = planning.digest(profile)
    mutate(path, "UPDATE history_profile SET profile_json=?", (store.canonical(profile),))
    mutate(path, "UPDATE history_semantics SET profile_digest=?", (profile["digest"],))
    assert initialize(path)["semantics"]["fresh"] == 2


def test_prepared_always_fresh_and_stale_prepared_v4_copy_refresh_preserves_recovery(tmp_path: Path) -> None:
    case = prepare_scenario(tmp_path)
    initialize(case.ledger)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected after durable prepare")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(state_writer, "publish_locked", interrupted)
        with pytest.raises(completion.CompletionInterruptedError):
            case.complete()
    with connection(case.ledger) as database:
        identity = database.execute("SELECT id FROM completion_intents").fetchone()[0]
        assert database.execute("SELECT count(*) FROM history_semantics").fetchone() == (0,)
    original_payload = completion_store._payload
    calls: list[str] = []

    def counted(envelope: Doc, binding: Doc) -> tuple[Doc, Doc, Doc]:
        calls.append(envelope["phase"])
        return original_payload(envelope, binding)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(completion_store, "_payload", counted)
        assert check(case.ledger).facts["semantics"] == {"bindings": 1, "fresh": 0, "reused": 0, "prepared": 1}
    assert calls == ["prepared"]
    stale_profile(case.ledger)
    original = source_rows(case.ledger)
    with connection(case.ledger) as database:
        assert index.source_facts(database)["semantics"]["prepared"] == 1
    initialize(case.ledger)
    assert source_rows(case.ledger) == original
    assert completion.recover(case.ledger, identity, state_path=case.state_path)["status"] == "aborted"
    assert check(case.ledger).facts["semantics"]["reused"] == 1


def test_exact_change_capability_rejects_same_row_payload_substitution_before_index_writes(tmp_path: Path) -> None:
    case = prepare_scenario(tmp_path)
    initialize(case.ledger)
    before = case.ledger.read_bytes()
    with connection(case.ledger, write=True) as database:
        checked = index.check(database)
        key = ("watermarks", next(iter(checked.watermarks)))
        captured = checked.capture([key])
        original = captured[key]
        assert original is not None
        authorized = (original[0], original[1] + 1, original[2])
        database.execute(
            "UPDATE watermarks SET serial=?,digest=? WHERE pending_key=?",
            (original[1] + 1, "sha256:" + "0" * 64, key[1]),
        )
        changes_before = database.total_changes
        with pytest.raises(history.HistoryError, match="exact authorized before/after"):
            index.maintain(database, checked, changes={key: index.Change(original, authorized)})
        assert database.total_changes == changes_before
    assert case.ledger.read_bytes() == before


def test_exact_change_capability_rejects_unlisted_rows_and_historical_deletion(ledger: Path) -> None:
    before = ledger.read_bytes()
    with connection(ledger, write=True) as database:
        checked = index.check(database)
        database.execute("DELETE FROM watermarks")
        with pytest.raises(history.HistoryError, match="cannot be deleted"):
            index.maintain(database, checked, changes={})
    assert ledger.read_bytes() == before
    with connection(ledger, write=True) as database:
        checked = index.check(database)
        database.execute("UPDATE watermarks SET serial=serial+1")
        with pytest.raises(history.HistoryError, match="outside the exact authorized"):
            index.maintain(database, checked, changes={})
    assert ledger.read_bytes() == before


def test_exact_change_capability_rejects_index_tampering_and_wrong_connection(ledger: Path) -> None:
    with connection(ledger, write=True) as database:
        checked = index.check(database)
        database.execute("DELETE FROM history_semantics")
        with pytest.raises(history.HistoryError, match="outside the owned mutation"):
            index.maintain(database, checked, changes={})
    with connection(ledger) as first, connection(ledger) as second:
        checked = index.check(first)
        with pytest.raises(history.HistoryError, match="same-connection"):
            index.maintain(second, checked, changes={})


def test_capture_cannot_adopt_already_changed_source_values_as_its_before_snapshot(ledger: Path) -> None:
    before = ledger.read_bytes()
    with connection(ledger, write=True) as database:
        checked = index.check(database)
        key = ("watermarks", next(iter(checked.watermarks)))
        database.execute("UPDATE watermarks SET serial=serial+1")
        with pytest.raises(history.HistoryError, match="changed before protected capture"):
            checked.capture([key])
    assert ledger.read_bytes() == before


def test_checked_public_maps_are_copies_and_rows_are_immutable(ledger: Path) -> None:
    with connection(ledger) as database:
        checked = index.check(database)
        checked.records.clear()
        checked.active.clear()
        checked.watermarks.clear()
        checked.facts.clear()
        snapshot = checked.snapshot
        snapshot[0].clear()
        assert checked.records and checked.active and checked.watermarks and checked.facts
        with pytest.raises(TypeError):
            checked.rows[("bad", "bad")] = ()  # type: ignore[index]


def test_shared_public_completion_validator_still_performs_full_fresh_semantics(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = completion_store._payload

    def counted(envelope: Doc, binding: Doc) -> tuple[Doc, Doc, Doc]:
        calls.append(envelope["phase"])
        return original(envelope, binding)

    monkeypatch.setattr(completion_store, "_payload", counted)
    with connection(ledger) as database:
        snapshot = store._base(database)
        completion_store.validate(database, *snapshot)
    assert sorted(calls) == ["aborted", "committed"]


def test_checked_retains_no_bulk_proof_bytes_and_capture_reads_only_selected_rows(tmp_path: Path) -> None:
    test_file, test_name = CASE.split("::")
    payload_size = 2 * 1024 * 1024
    report = (
        f'<testsuite><testcase file="{test_file}" name="{test_name}"><system-out>'.encode()
        + b"x" * payload_size
        + b"</system-out></testcase></testsuite>"
    )
    case = prepare_scenario(tmp_path, source=Source(report=report))
    assert case.complete()["status"] == "completed"
    initialize(case.ledger)
    script = """
import gc
import json
import sqlite3
import sys
import tracemalloc
from contextlib import closing
from exacly import execution_index as index

with closing(sqlite3.connect(sys.argv[1], isolation_level=None)) as database:
    database.execute("BEGIN")
    # Warm process-wide parser/profile caches outside the retained-view measurement. Both
    # checks still read the actual complete 2 MiB proof; a bulk Checked copy remains live.
    warm = index.check(database)
    del warm
    gc.collect()
    tracemalloc.start()
    checked = index.check(database)
    gc.collect()
    retained, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert retained < int(sys.argv[2]) // 2, retained
    assert not checked.rows
    key = ("watermarks", next(iter(checked.watermarks)))
    captured = checked.capture([key, ("bindings", "absent-new-binding")])
    assert set(captured) == {key, ("bindings", "absent-new-binding")}
    assert captured[key] is not None
    assert captured[("bindings", "absent-new-binding")] is None
    assert set(checked.rows) == set(captured)
    print(json.dumps({"retained": retained}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(case.ledger), str(payload_size)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["retained"] < payload_size // 2

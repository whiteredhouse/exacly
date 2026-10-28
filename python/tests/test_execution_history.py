"""Required history baselines are checked before every fresh, read-only semantic audit."""

from __future__ import annotations

import copy
import importlib.metadata as metadata
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest
from exacly import completion, completion_store, planning, state_writer
from exacly import execution_history as history
from exacly import execution_store as store
from test_completion import prepare_scenario

Doc = dict[str, Any]


def source_columns() -> list[tuple[str, str]]:
    """Read the actual source schema independently of the history coverage registry."""
    schema = {**store.SCHEMA, **completion_store.SCHEMA}
    with closing(sqlite3.connect(":memory:")) as database:
        for statement in schema.values():
            database.execute(statement)
        return [(table, column[1]) for table in schema for column in database.execute(f"PRAGMA table_info({table})")]


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, isolation_level=None)) as database:
        database.execute("BEGIN")
        try:
            yield database
        finally:
            if database.in_transaction:
                database.rollback()


def mutate(path: Path, statement: str, parameters: tuple[Any, ...] = ()) -> None:
    with closing(sqlite3.connect(path, isolation_level=None)) as database:
        database.execute(statement, parameters)


def source_rows(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    with connection(path) as database:
        return {table: database.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in history._FIELDS}


def initialize(path: Path) -> Doc:
    with closing(sqlite3.connect(path, isolation_level=None)) as database:
        database.execute("PRAGMA synchronous=EXTRA")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("BEGIN IMMEDIATE")
        try:
            result = history.initialize(database)
            database.commit()
            return result
        finally:
            if database.in_transaction:
                database.rollback()


@pytest.fixture(scope="module")
def v2(tmp_path_factory: pytest.TempPathFactory) -> Path:
    case = prepare_scenario(tmp_path_factory.mktemp("history-source"))

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected before publication")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(state_writer, "publish_locked", interrupted)
        with pytest.raises(completion.CompletionInterruptedError):
            case.complete()
    with connection(case.ledger) as database:
        intent = database.execute("SELECT id FROM completion_intents").fetchone()[0]
    assert completion.recover(case.ledger, intent, state_path=case.state_path)["status"] == "aborted"
    assert case.complete()["status"] == "completed"
    return case.ledger


@pytest.fixture
def ledger(tmp_path: Path, v2: Path) -> Path:
    private = tmp_path / "operator"
    private.mkdir(mode=0o700)
    result = private / "history.sqlite3"
    shutil.copyfile(v2, result)
    result.chmod(0o600)
    return result


def audit(path: Path) -> Doc:
    with store.locked(path, write=False) as (absolute, _), history.database(absolute) as database:
        return history.validate(database)


def test_initialize_preserves_all_source_cells_and_audit_is_fresh_read_only(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = source_rows(ledger)
    facts = initialize(ledger)
    assert source_rows(ledger) == original
    assert facts["format_version"] == 3
    assert facts["semantics"] == {"bindings": 1, "fresh": 2, "reused": 0}
    assert facts["index_rows"] == {"integrity": 6, "semantic": 2}
    assert facts["integrity"]["complete"] is True
    assert facts["integrity"]["cells"] == sum(len(row) for rows in original.values() for row in rows)
    assert facts["integrity"]["bytes"] > 0
    before = ledger.read_bytes()
    calls: list[str] = []
    original_payload = completion_store._payload

    def counted(envelope: Doc, binding: Doc) -> tuple[Doc, Doc, Doc]:
        calls.append(envelope["phase"])
        return original_payload(envelope, binding)

    monkeypatch.setattr(completion_store, "_payload", counted)
    assert audit(ledger) == facts
    assert sorted(calls) == ["aborted", "committed"]
    assert ledger.read_bytes() == before
    assert set(item.name for item in ledger.parent.iterdir()) == {ledger.name}


def test_read_only_source_facts_use_the_identical_initialization_gate_without_baseline_claim(ledger: Path) -> None:
    before = ledger.read_bytes()
    with store.database(ledger, create=False) as (database, _):
        database.execute("BEGIN")
        source = history.source_facts(database)
    assert ledger.read_bytes() == before
    assert source["format_version"] == 2
    assert source["integrity_baseline_checked"] is False
    converted = initialize(ledger)
    assert source["profile"] == converted["profile"]
    assert source["semantics"] == converted["semantics"]
    assert source["coverage"] == {key: converted["integrity"][key] for key in ("rows", "cells", "bytes", "complete")}


def test_typed_transcript_has_no_concatenation_type_domain_or_presence_aliases() -> None:
    candidates = [
        history._transcript("one", [("left", "text", b"a"), ("right", "text", b"bc")]),
        history._transcript("one", [("left", "text", b"ab"), ("right", "text", b"c")]),
        history._transcript("two", [("left", "text", b"a"), ("right", "text", b"bc")]),
        history._transcript("one", [("left", "blob", b"a"), ("right", "text", b"bc")]),
        history._transcript("one", [("different", "text", b"a"), ("right", "text", b"bc")]),
        history._transcript("one", [("right", "text", b"bc"), ("left", "text", b"a")]),
        history._part("receipt", "blob", None),
        history._part("receipt", "blob", None, present=False),
        history._part("receipt", "blob", b""),
    ]
    assert len(set(candidates)) == len(candidates)


@pytest.mark.parametrize("table,column", source_columns())
def test_every_retained_source_cell_is_covered_before_semantics(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, table: str, column: str
) -> None:
    initialize(ledger)
    if column == "phase":
        value: Any = "aborted"
        condition = "phase='committed'"
    elif column == "serial":
        value, condition = 999, "1"
    else:
        value, condition = b"changed retained octets", "1"
    mutate(
        ledger,
        f"UPDATE {table} SET {column}=? WHERE rowid=(SELECT rowid FROM {table} WHERE {condition} LIMIT 1)",
        (value,),
    )
    before = ledger.read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("corrupt baseline reached semantic interpretation")

    monkeypatch.setattr(history, "_fresh", forbidden)
    with pytest.raises(store.StoreError):
        audit(ledger)
    assert ledger.read_bytes() == before


@pytest.mark.parametrize(
    "table,column",
    [("bindings", "record_json"), ("completion_intents", "intent_json"), ("completion_receipts", "receipt_json")],
)
def test_same_octets_in_wrong_sqlite_storage_class_fail(ledger: Path, table: str, column: str) -> None:
    initialize(ledger)
    mutate(ledger, f"UPDATE {table} SET {column}=CAST({column} AS BLOB)")
    with pytest.raises(history.HistoryError, match="invalid type or size"):
        audit(ledger)


@pytest.mark.parametrize(
    "attack",
    ["missing_baseline", "extra_baseline", "changed_hash", "removed_proof_and_baseline", "roster", "real_counter"],
)
def test_required_roster_cannot_be_treated_as_a_cache_miss(ledger: Path, attack: str) -> None:
    initialize(ledger)
    if attack == "missing_baseline":
        mutate(
            ledger,
            "DELETE FROM history_integrity WHERE table_name='completion_intents' "
            "AND row_key=(SELECT id FROM completion_intents WHERE phase='aborted')",
        )
    elif attack == "extra_baseline":
        mutate(ledger, "INSERT INTO history_integrity VALUES ('bindings','extra',?,0,4)", ("sha256:" + "0" * 64,))
    elif attack == "changed_hash":
        mutate(ledger, "UPDATE history_integrity SET row_digest=?", ("sha256:" + "0" * 64,))
    elif attack == "removed_proof_and_baseline":
        with connection(ledger) as database:
            identity = database.execute("SELECT id FROM completion_intents WHERE phase='aborted'").fetchone()[0]
        mutate(ledger, "DELETE FROM history_semantics WHERE intent_id=?", (identity,))
        mutate(ledger, "DELETE FROM completion_intents WHERE id=?", (identity,))
        mutate(ledger, "DELETE FROM history_integrity WHERE table_name='completion_intents' AND row_key=?", (identity,))
    elif attack == "roster":
        mutate(ledger, "UPDATE history_profile SET roster_digest=?", ("sha256:" + "0" * 64,))
    else:
        mutate(ledger, "UPDATE history_profile SET row_count=1.5")
    before = ledger.read_bytes()
    with pytest.raises(history.HistoryError, match=r"baseline|roster|invalid type"):
        audit(ledger)
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
def test_present_semantic_verdict_cannot_disagree_with_fresh_result(ledger: Path, column: str, value: str) -> None:
    initialize(ledger)
    mutate(ledger, f"UPDATE history_semantics SET {column}=?", (value,))
    before = ledger.read_bytes()
    with pytest.raises(history.HistoryError, match="semantic verdict"):
        audit(ledger)
    assert ledger.read_bytes() == before


def test_missing_optional_semantic_verdicts_get_fresh_audit_without_repair(ledger: Path) -> None:
    initialize(ledger)
    mutate(ledger, "DELETE FROM history_semantics")
    before = ledger.read_bytes()
    result = audit(ledger)
    assert result["semantics"] == {"bindings": 1, "fresh": 2, "reused": 0}
    assert result["index_rows"]["semantic"] == 0
    assert ledger.read_bytes() == before


@pytest.mark.parametrize(
    "table,column,expression",
    [
        ("history_profile", "profile_json", "zeroblob(8388609)"),
        ("history_semantics", "verdict_json", "zeroblob(8388609)"),
        ("completion_intents", "before_state", "zeroblob(16777217)"),
        ("bindings", "record_json", "CAST(x'80' AS TEXT)"),
    ],
)
def test_corrupt_cells_fail_closed_with_bounded_controlled_errors(
    ledger: Path, table: str, column: str, expression: str
) -> None:
    initialize(ledger)
    mutate(ledger, f"UPDATE {table} SET {column}={expression}")
    with pytest.raises(history.HistoryError, match=r"invalid type or size|UTF-8"):
        audit(ledger)


def test_unknown_profile_is_unsupported_after_required_integrity_check(ledger: Path) -> None:
    initialize(ledger)
    with connection(ledger) as database:
        retained = json.loads(database.execute("SELECT profile_json FROM history_profile").fetchone()[0])
    retained.pop("digest")
    retained["family"] = "future-unknown-profile"
    retained["digest"] = planning.digest(retained)
    mutate(ledger, "UPDATE history_profile SET profile_json=?", (store.canonical(retained),))
    with pytest.raises(history.UnsupportedProfileError):
        audit(ledger)
    mutate(ledger, "UPDATE history_integrity SET row_digest=?", ("sha256:" + "0" * 64,))
    with pytest.raises(history.HistoryError, match="baseline") as raised:
        audit(ledger)
    assert not isinstance(raised.value, history.UnsupportedProfileError)


def test_coherently_resealed_invalid_binding_still_fails_full_fresh_semantics(ledger: Path) -> None:
    initialize(ledger)
    with connection(ledger) as database:
        binding = json.loads(database.execute("SELECT record_json FROM bindings").fetchone()[0])
    binding["contract"]["goal"]["criteria_digest"] = "sha256:" + "0" * 64
    mutate(ledger, "UPDATE bindings SET record_json=?", (store.canonical(binding),))
    # Model the explicitly excluded same-user rewrite threat. Matching replacement byte hashes
    # cannot make the altered contract pass the mandatory fresh semantic audit.
    with closing(sqlite3.connect(ledger, isolation_level=None)) as database:
        database.execute("BEGIN IMMEDIATE")
        rows = history._rows(database, None)
        database.execute("DELETE FROM history_integrity")
        for row in rows:
            database.execute(
                "INSERT INTO history_integrity VALUES (?,?,?,?,?)",
                tuple(row[key] for key in ("table_name", "row_key", "row_digest", "byte_count", "cell_count")),
            )
        database.execute(
            "UPDATE history_profile SET roster_digest=?,row_count=?,cell_count=?,byte_count=?",
            (
                history._roster(rows),
                len(rows),
                sum(row["cell_count"] for row in rows),
                sum(row["byte_count"] for row in rows),
            ),
        )
        database.commit()
    with pytest.raises(history.HistoryError):
        audit(ledger)


def test_new_unreferenced_binding_contract_receives_fresh_validation(tmp_path: Path) -> None:
    case = prepare_scenario(tmp_path)
    with connection(case.ledger) as database:
        original = store.validate(database)[0][case.binding["id"]]
    changed = copy.deepcopy(original)
    changed["contract"]["goal"]["criteria_digest"] = "sha256:" + "0" * 64
    mutate(case.ledger, "UPDATE bindings SET record_json=?", (store.canonical(changed),))
    before = case.ledger.read_bytes()
    with pytest.raises(history.HistoryError):
        initialize(case.ledger)
    assert case.ledger.read_bytes() == before


def test_initialization_and_audit_require_explicit_coherent_transactions(ledger: Path) -> None:
    with (
        closing(sqlite3.connect(ledger, isolation_level=None)) as database,
        pytest.raises(history.HistoryError, match="transaction"),
    ):
        history.initialize(database)
    initialize(ledger)
    with (
        closing(sqlite3.connect(ledger, isolation_level=None)) as database,
        pytest.raises(history.HistoryError, match="transaction"),
    ):
        history.validate(database)


def test_audit_only_format_is_rejected_by_ordinary_read_write_and_validate(ledger: Path) -> None:
    initialize(ledger)
    before = ledger.read_bytes()
    with connection(ledger) as database:
        binding_id = database.execute("SELECT id FROM bindings").fetchone()[0]
        with pytest.raises(store.StoreError, match="audit-only"):
            store.validate(database)
    with pytest.raises(store.StoreError, match="audit-only"):
        store.get(ledger, binding_id)
    with pytest.raises(store.StoreError, match="audit-only"), store.database(ledger, create=True):
        pass
    assert ledger.read_bytes() == before


def test_terminal_dependency_closure_excludes_unrelated_rows_and_binds_receipt_absence(ledger: Path) -> None:
    initialize(ledger)
    with connection(ledger) as database:
        rows = history._rows(database, None)
        _, verdicts = history._fresh(database, None)
    lookup = {(row["table_name"], row["row_key"]): row for row in rows}
    for identity, verdict in verdicts.items():
        original = history._dependencies(lookup, identity, verdict)
        changed = copy.deepcopy(lookup)
        watermark_key = next(key for key in changed if key[0] == "watermarks")
        changed[watermark_key]["row_digest"] = "sha256:" + "0" * 64
        assert history._dependencies(changed, identity, verdict) == original
        changed[("completion_intents", identity)]["row_digest"] = "sha256:" + "0" * 64
        assert history._dependencies(changed, identity, verdict) != original
        if verdict["phase"] == "aborted":
            assert ("completion_receipts", identity) not in lookup
        else:
            missing = dict(lookup)
            missing.pop(("completion_receipts", identity))
            with pytest.raises(history.HistoryError, match="receipt association"):
                history._dependencies(missing, identity, verdict)


def test_profile_binds_trusted_code_schema_runtime_and_format_checker_inputs() -> None:
    profile = history.profile()
    payload = {key: value for key, value in profile.items() if key != "digest"}
    assert profile["digest"] == planning.digest(payload)
    inputs = {item["name"]: item for item in profile["inputs"]}
    assert {"exacly/python", "exacly/schemas", "runtime/python", "dependency/jsonschema", "dependency/pyyaml"} <= set(
        inputs
    )
    assert any(name.startswith("dependency-code/jsonschema/") for name in inputs)
    assert profile["format_checkers"]
    assert profile["runtime"]["sqlite_source"]
    assert all(store.DIGEST.fullmatch(item["digest"]) and item["size"] > 0 for item in inputs.values())


def test_profile_missing_mandatory_metadata_fails_instead_of_omitting_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = metadata.distribution

    def missing(name: str) -> Any:
        if name == "attrs":
            raise metadata.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(metadata, "distribution", missing)
    with pytest.raises(history.HistoryError, match="dependency is missing: attrs"):
        history.profile()


def test_non_utf8_sqlite_source_is_rejected_with_controlled_error(tmp_path: Path) -> None:
    path = tmp_path / "utf16.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as database:
        database.execute("PRAGMA encoding='UTF-16le'")
        database.execute("BEGIN IMMEDIATE")
        database.execute("CREATE TABLE marker (value TEXT)")
        with pytest.raises(history.HistoryError, match="UTF-8 SQLite text encoding"):
            history.initialize(database)
        with pytest.raises(history.HistoryError, match="UTF-8 SQLite text encoding"):
            history.source_facts(database)
        database.rollback()


def test_sqlite_lock_timeout_is_operational_and_never_reported_as_corrupt(ledger: Path) -> None:
    initialize(ledger)
    with closing(sqlite3.connect(ledger, isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with (
                closing(sqlite3.connect(ledger, isolation_level=None)) as reader,
                pytest.raises(store.LockTimeoutError, match="lock timed out"),
            ):
                reader.execute("BEGIN")
                history.validate(reader, deadline=store.Deadline(0))
        finally:
            holder.rollback()

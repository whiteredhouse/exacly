"""A validated journal is private to one protected, unchanged SQLite operation."""

from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any

import pytest
from exacly import completion, completion_store, planning
from exacly import execution_store as store
from test_completion_faults import FaultScenario
from test_completion_store import database, intent, proof
from test_completion_store import prepared as prepared

Doc = dict[str, Any]
OPERATIONS = (
    "envelope",
    "completed_for_binding",
    "find_completed",
    "current",
    "get",
    "prepare",
    "finalize",
    "abort",
)


def invoke(journal: completion_store.Journal, case: FaultScenario, retained: Doc, operation: str) -> Any:
    identity = retained["intent"]["id"]
    if operation == "envelope":
        return journal.envelope(case.binding["id"])
    if operation == "completed_for_binding":
        return journal.completed_for_binding(case.binding["id"])
    if operation == "find_completed":
        return journal.find_completed(retained["intent"]["request_digest"], binding_id=case.binding["id"])
    if operation == "current":
        return journal.current()
    if operation == "get":
        return journal.get(identity)
    if operation == "prepare":
        return journal.prepare(intent(retained), **proof(retained))
    if operation == "finalize":
        return journal.finalize(identity, completion._receipt(retained["intent"], case.binding))
    if operation == "abort":
        return journal.abort(identity)
    raise AssertionError(f"unknown test operation {operation}")


def without_pragma(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    original_connect = sqlite3.connect

    class UnsupportedPragma(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == f"PRAGMA {name}":
                return super().execute("PRAGMA exacly_test_unsupported_pragma")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=UnsupportedPragma)

    monkeypatch.setattr(sqlite3, "connect", connect)


@pytest.mark.parametrize("operation", ["get", "abort"])
def test_an_absent_optional_pragma_preserves_owned_writes_and_raw_change_guards(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    case, retained = prepared
    without_pragma(monkeypatch, "trusted_schema")
    with database(case) as connection, completion_store.validated(connection) as journal:
        assert journal.get(retained["intent"]["id"]) == retained
        assert journal.abort(retained["intent"]["id"])["phase"] == "aborted"
        replacement = journal.prepare(intent(retained), **proof(retained))
        assert journal.current() == replacement
        connection.execute("UPDATE completion_intents SET phase='aborted' WHERE id=?", (replacement["intent"]["id"],))
        before = case.ledger.read_bytes()
        with pytest.raises(store.StoreError):
            invoke(journal, case, replacement, operation)
        assert case.ledger.read_bytes() == before


def test_an_absent_required_change_guard_fails_closed_with_a_store_error(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, _ = prepared
    without_pragma(monkeypatch, "data_version")
    before = case.ledger.read_bytes()
    with (
        database(case) as connection,
        pytest.raises(store.StoreError, match="required completion journal guard: data_version"),
        completion_store.validated(connection),
    ):
        pytest.fail("a missing change guard cannot establish a trusted view")
    assert case.ledger.read_bytes() == before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_journal_cannot_outlive_its_validated_scope(prepared: tuple[FaultScenario, Doc], operation: str) -> None:
    case, retained = prepared
    before = case.ledger.read_bytes()
    with database(case) as connection:
        with completion_store.validated(connection) as journal:
            assert journal.get(retained["intent"]["id"]) == retained
        with pytest.raises(store.StoreError):
            invoke(journal, case, retained, operation)
    assert case.ledger.read_bytes() == before


@pytest.mark.parametrize("operation", ["get", "current", "envelope"])
def test_nested_return_values_cannot_mutate_the_validated_view(
    prepared: tuple[FaultScenario, Doc], operation: str
) -> None:
    case, retained = prepared
    with database(case) as connection, completion_store.validated(connection) as journal:
        expected = copy.deepcopy(invoke(journal, case, retained, operation))
        changed = invoke(journal, case, retained, operation)
        if operation == "envelope":
            changed["binding"]["contract"]["pending"]["record"] = "UNTRUSTED"
            changed["state_watermark"]["serial"] = -1
            changed["completion"]["phase"] = "committed"
        else:
            changed["intent"]["request"]["run_id"] = -1
            changed["model_documents"].clear()
            changed["admitted_state"]["pending"] = [{"record": "UNTRUSTED"}]
        assert invoke(journal, case, retained, operation) == expected


def test_owned_mutations_refresh_phases_receipts_and_watermarks_in_the_same_scope(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    case, retained = prepared
    old_id = retained["intent"]["id"]
    before_state = json.loads(retained["before_state"])
    with database(case) as connection, completion_store.validated(connection) as journal:
        aborted = journal.abort(old_id)
        assert aborted["phase"] == "aborted"
        assert journal.get(old_id)["phase"] == "aborted"
        assert journal.current() is None
        envelope = journal.envelope(case.binding["id"])
        assert envelope["completion"] is None and envelope["completed_receipt"] is None
        assert envelope["state_watermark"] == {
            "serial": before_state["serial"],
            "digest": planning.digest(before_state),
        }

        proposed = intent(retained)
        replacement = journal.prepare(proposed, **proof(retained))
        new_id = replacement["intent"]["id"]
        assert new_id != old_id
        assert journal.current() == replacement
        assert journal.envelope(case.binding["id"])["completion"]["id"] == new_id

        receipt = completion._receipt(replacement["intent"], case.binding)
        assert journal.finalize(new_id, receipt) == receipt
        assert journal.current() is None
        assert journal.get(new_id)["phase"] == "committed"
        assert journal.get(old_id)["phase"] == "aborted"
        assert journal.completed_for_binding(case.binding["id"]) == receipt
        assert journal.find_completed(receipt["request_digest"], binding_id=case.binding["id"]) == receipt
        envelope = journal.envelope(case.binding["id"])
        after = json.loads(retained["after_state"])
        assert envelope["completion"]["phase"] == "committed"
        assert envelope["state_watermark"] == {"serial": after["serial"], "digest": planning.digest(after)}

        # Receipt-returning APIs must also isolate their nested policy/claim/state documents.
        for getter in (
            lambda: journal.completed_for_binding(case.binding["id"]),
            lambda: journal.find_completed(receipt["request_digest"]),
            lambda: journal.envelope(case.binding["id"])["completed_receipt"],
        ):
            returned = getter()
            assert returned is not None
            returned["claims"]["operator_authorized"] = False
            returned["state"]["after_serial"] = -1
            assert getter() == receipt


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE completion_intents SET phase=phase",
        "PRAGMA application_id=0",
        "PRAGMA user_version=0",
        "PRAGMA schema_version=9999",
        "PRAGMA journal_mode=MEMORY",
        "CREATE INDEX unexpected_index ON completion_intents(phase)",
        "CREATE TEMP TABLE completion_intents (id TEXT)",
        "ATTACH ':memory:' AS unexpected_database",
        "BEGIN",
    ],
)
def test_unexpected_raw_sql_invalidates_even_an_otherwise_unchanged_view(
    prepared: tuple[FaultScenario, Doc], statement: str
) -> None:
    case, retained = prepared
    with database(case) as connection, completion_store.validated(connection) as journal:
        connection.execute(statement)
        after_raw_change = case.ledger.read_bytes()
        with pytest.raises(store.StoreError):
            journal.get(retained["intent"]["id"])
        # Once invalidated, ending an unexpected transaction must not resurrect the old authority.
        if connection.in_transaction:
            connection.rollback()
        with pytest.raises(store.StoreError):
            journal.abort(retained["intent"]["id"])
        assert case.ledger.read_bytes() == after_raw_change


def test_a_rolled_back_raw_write_does_not_resurrect_the_validated_view(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    case, retained = prepared
    before = case.ledger.read_bytes()
    with database(case) as connection, completion_store.validated(connection) as journal:
        connection.execute("BEGIN")
        connection.execute("UPDATE completion_intents SET phase='aborted'")
        connection.rollback()
        assert case.ledger.read_bytes() == before
        with pytest.raises(store.StoreError):
            journal.get(retained["intent"]["id"])


def test_an_external_sqlite_commit_cannot_be_adopted_by_a_validated_scope(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    case, retained = prepared
    with database(case) as connection, completion_store.validated(connection) as journal:
        with sqlite3.connect(case.ledger) as external:
            # Even a valid external transition requires a new full validation scope.
            external.execute("UPDATE completion_intents SET phase='aborted'")
        before = case.ledger.read_bytes()
        with pytest.raises(store.StoreError):
            journal.get(retained["intent"]["id"])
        with pytest.raises(store.StoreError):
            journal.abort(retained["intent"]["id"])
        assert case.ledger.read_bytes() == before


@pytest.mark.parametrize(
    "statement", ["CREATE TEMP TABLE completion_intents (id TEXT)", "ATTACH ':memory:' AS unexpected_database"]
)
def test_a_validation_scope_rejects_an_already_extended_connection_environment(
    prepared: tuple[FaultScenario, Doc], statement: str
) -> None:
    case, _ = prepared
    with database(case) as connection:
        connection.execute(statement)
        with pytest.raises(store.StoreError), completion_store.validated(connection):
            pytest.fail("an attached or shadowing schema must not enter a trusted journal scope")


@pytest.mark.parametrize("boundary", ["read", "begin_write"])
def test_an_external_commit_during_an_operation_is_not_silently_adopted(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    case, retained = prepared
    original_connect = sqlite3.connect
    armed = False
    triggered = False

    class Interleaved(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            nonlocal armed, triggered
            selected = (
                sql.startswith("SELECT length(CAST(intent_json") if boundary == "read" else sql == "BEGIN IMMEDIATE"
            )
            if armed and selected:
                armed = False
                triggered = True
                with original_connect(case.ledger) as external:
                    external.execute("UPDATE completion_intents SET phase='aborted'")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=Interleaved)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with database(case) as connection:
        with completion_store.validated(connection) as journal:
            armed = True
            with pytest.raises(store.StoreError):
                if boundary == "read":
                    journal.get(retained["intent"]["id"])
                else:
                    journal.abort(retained["intent"]["id"])
            assert triggered, "the external writer must run after the initial freshness check"
            with pytest.raises(store.StoreError):
                journal.get(retained["intent"]["id"])
        with completion_store.validated(connection) as fresh:
            assert fresh.get(retained["intent"]["id"])["phase"] == "aborted"


@pytest.mark.parametrize("boundary", ["scope_entry", "after_owned_mutation"])
def test_a_commit_during_full_validation_cannot_be_blessed_as_a_new_snapshot(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    case, retained = prepared
    original_validate = store.validate
    triggered = False

    def interleaved(
        connection: sqlite3.Connection, *, deadline: store.Deadline | None = None
    ) -> tuple[dict[str, Doc], dict[str, str], dict[str, Doc]]:
        nonlocal triggered
        result = original_validate(connection, deadline=deadline)
        if not triggered:
            triggered = True
            # This row has already been checked. The new token cannot bless its unchecked bytes.
            with sqlite3.connect(case.ledger) as external:
                external.execute("UPDATE bindings SET record_json=record_json || ' '")
        return result

    with database(case) as connection:
        if boundary == "scope_entry":
            monkeypatch.setattr(store, "validate", interleaved)
            with pytest.raises(store.StoreError), completion_store.validated(connection):
                pytest.fail("validation spanning an external commit cannot establish a trusted view")
        else:
            with completion_store.validated(connection) as journal:
                monkeypatch.setattr(store, "validate", interleaved)
                with pytest.raises(store.StoreError):
                    journal.abort(retained["intent"]["id"])
                with pytest.raises(store.StoreError):
                    journal.get(retained["intent"]["id"])
    assert triggered


@pytest.mark.parametrize("failure", ["statement", "before_commit", "after_commit"])
def test_a_failed_owned_transaction_poisons_the_scope_until_fresh_validation(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    case, retained = prepared
    receipt = completion._receipt(retained["intent"], case.binding)
    original_connect = sqlite3.connect

    class Interrupted(sqlite3.Connection):
        fired = False

        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if failure == "statement" and sql.startswith("UPDATE watermarks") and not self.fired:
                self.fired = True
                raise sqlite3.OperationalError("injected transaction interruption")
            return super().execute(sql, parameters)

        def commit(self) -> None:
            if failure == "before_commit" and not self.fired:
                self.fired = True
                raise sqlite3.OperationalError("injected precommit interruption")
            super().commit()
            if failure == "after_commit" and not self.fired:
                self.fired = True
                raise sqlite3.OperationalError("injected postcommit interruption")

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=Interrupted)

    monkeypatch.setattr(sqlite3, "connect", connect)
    before = case.ledger.read_bytes()
    with database(case) as connection:
        with completion_store.validated(connection) as journal:
            with pytest.raises((sqlite3.Error, store.StoreError), match="injected"):
                journal.finalize(retained["intent"]["id"], receipt)
            with pytest.raises(store.StoreError):
                journal.get(retained["intent"]["id"])
            with pytest.raises(store.StoreError):
                journal.abort(retained["intent"]["id"])
        # A new scope sees only the actual durable outcome, not the failed object's cached phase.
        with completion_store.validated(connection) as fresh:
            value = fresh.get(retained["intent"]["id"])
            assert value["phase"] == ("committed" if failure == "after_commit" else "prepared")
            assert value["receipt"] == (receipt if failure == "after_commit" else None)
    if failure != "after_commit":
        assert case.ledger.read_bytes() == before

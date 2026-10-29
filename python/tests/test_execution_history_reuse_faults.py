"""Indexed mutations retain the journal's exact-input, atomicity, and poisoned-scope boundaries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest
import test_completion_journal as journal_tests
from exacly import (
    completion,
    completion_store,
    execution_index,
    history_maintenance,
    planning,
    reconciliation,
    state_writer,
)
from exacly import execution_store as store
from test_admission import ATTEMPT, READINESS, RUN_ID
from test_applicability import WHEN, B
from test_completion_faults import FaultScenario, NoNetwork, prepare_case
from test_completion_store import database, intent, proof
from test_reconciliation import selected

Doc = dict[str, Any]


@pytest.fixture
def case(tmp_path: Path) -> FaultScenario:
    value = prepare_case(tmp_path)
    destination = tmp_path / "operational-operator"
    report = history_maintenance.activate(value.ledger, destination)
    assert report["status"] == "activated", report
    value.ledger = destination / "operational/ledger.sqlite3"
    return value


@pytest.fixture
def prepared(case: FaultScenario, monkeypatch: pytest.MonkeyPatch) -> tuple[FaultScenario, Doc]:
    def unpublished(*args: Any, **kwargs: Any) -> None:
        raise OSError("stop after durable indexed preparation")

    state = case.state_path.read_bytes()
    with monkeypatch.context() as patched:
        patched.setattr(state_writer, "publish_locked", unpublished)
        with pytest.raises(completion.CompletionInterruptedError):
            case.complete()
    assert case.state_path.read_bytes() == state
    with database(case) as connection:
        retained = completion_store.current(connection)
    assert retained is not None and retained["phase"] == "prepared"
    return case, retained


def assert_auditable(case: FaultScenario) -> None:
    report = history_maintenance.audit(case.ledger)
    assert report["status"] == "passed", report
    assert report["validation"]["semantics"]["reused"] == 0


def test_indexed_completion_reports_name_one_protected_stage_and_preserve_historical_receipts(
    case: FaultScenario,
) -> None:
    completed = case.complete()
    assert completed["status"] == "completed"
    jsonschema.validate(completed, completion.SCHEMA)
    assert completed["schema_version"] == "0.2.0"
    assert completed["receipt"]["schema_version"] == "0.1.0"
    view = completed["history_validation"]
    assert view["scope"] == "protected_view_at_stage_entry" and view["stage"] == "commit"
    assert view["facts"]["semantics"] == {"bindings": 1, "fresh": 0, "reused": 0, "prepared": 0}
    state, ledger = case.state_path.read_bytes(), case.ledger.read_bytes()
    operations: tuple[tuple[Callable[[], Doc], str], ...] = (
        (lambda: completion.show(case.ledger, completed["completion_id"]), "show"),
        (lambda: case.complete(source=NoNetwork()), "pre_admission"),
        (lambda: case.recover(completed["completion_id"]), "recovery_preview"),
    )
    for operation, stage in operations:
        with patch.object(execution_index, "check", wraps=execution_index.check) as observed:
            report = operation()
        assert observed.call_count == 1
        jsonschema.validate(report, completion.SCHEMA)
        assert report["schema_version"] == "0.2.0"
        assert report["receipt"] == completed["receipt"]
        history_view = report["history_validation"]
        assert history_view["scope"] == "protected_view_at_stage_entry" and history_view["stage"] == stage
        facts = history_view["facts"]
        assert facts["profile_status"] == "current" and facts["integrity"]["complete"] is True
        assert facts["semantics"] == {"bindings": 1, "fresh": 0, "reused": 1, "prepared": 0}
        assert report["digest"] == planning.digest({key: value for key, value in report.items() if key != "digest"})
        assert "History at" in completion.as_text(report)
        assert case.state_path.read_bytes() == state and case.ledger.read_bytes() == ledger


def test_recovery_reports_its_prepared_stage_entry_without_claiming_post_mutation_counts(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    case, retained = prepared
    report = case.recover(retained["intent"]["id"])
    jsonschema.validate(report, completion.SCHEMA)
    assert report["status"] == "aborted" and report["schema_version"] == "0.2.0"
    view = report["history_validation"]
    assert view["scope"] == "protected_view_at_stage_entry" and view["stage"] == "recovery_commit"
    assert view["facts"]["semantics"] == {"bindings": 1, "fresh": 0, "reused": 0, "prepared": 1}
    assert_auditable(case)


def test_bound_preview_reports_the_freshly_checked_binding_view_and_remains_read_only(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    case, retained = prepared
    assert case.recover(retained["intent"]["id"])["status"] == "aborted"
    state, ledger = case.state_path.read_bytes(), case.ledger.read_bytes()
    with patch.object(execution_index, "check", wraps=execution_index.check) as observed:
        report = reconciliation.preview(
            case.source,
            case.plan,
            case.plan["actions"][0]["id"],
            READINESS,
            case.source.policy,
            selected(B),
            RUN_ID,
            ATTEMPT,
            WHEN,
            ledger=case.ledger,
            binding_id=case.binding["id"],
            state_doc=json.loads(state),
        )
    assert observed.call_count == 2
    jsonschema.validate(report, reconciliation.SCHEMA)
    assert report["status"] == "ready"
    view = report["binding"]["history_validation"]
    assert view["scope"] == "protected_view_at_stage_entry" and view["stage"] == "binding_recheck"
    assert view["facts"]["integrity"]["complete"] is True
    assert view["facts"]["semantics"] == {"bindings": 1, "fresh": 0, "reused": 1, "prepared": 0}
    assert case.state_path.read_bytes() == state and case.ledger.read_bytes() == ledger


def test_journal_validation_facts_are_isolated_guarded_and_do_not_run_an_extra_check(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    case, _ = prepared
    with database(case) as connection, patch.object(execution_index, "check", wraps=execution_index.check) as observed:
        with completion_store.validated(connection) as journal:
            facts = journal.validation
            assert facts is not None
            facts["semantics"]["prepared"] = -1
            assert journal.validation is not None and journal.validation["semantics"]["prepared"] == 1
            assert observed.call_count == 1
            connection.execute("UPDATE completion_intents SET phase=phase")
            with pytest.raises(store.StoreError, match="changed"):
                _ = journal.validation
        with pytest.raises(store.StoreError, match="closed"):
            _ = journal.validation


def test_abort_retry_finalize_refreshes_baselines_verdicts_and_receipt_in_one_scope(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    journal_tests.test_owned_mutations_refresh_phases_receipts_and_watermarks_in_the_same_scope(prepared)
    assert_auditable(prepared[0])


@pytest.mark.parametrize("failure", ["statement", "before_commit", "after_commit"])
def test_failed_indexed_transactions_poison_the_scope_and_leave_only_a_coherent_durable_outcome(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    journal_tests.test_a_failed_owned_transaction_poisons_the_scope_until_fresh_validation(
        prepared, monkeypatch, failure
    )
    monkeypatch.undo()
    assert_auditable(prepared[0])


@pytest.mark.parametrize("operation", ["prepare", "finalize", "abort"])
def test_same_row_substitution_cannot_be_blessed_by_index_maintenance(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    case, retained = prepared
    identity = retained["intent"]["id"]
    with database(case) as connection, completion_store.validated(connection) as journal:
        if operation == "prepare":
            journal.abort(identity)
        before = case.ledger.read_bytes()
        original = completion_store._execute
        injected = False

        def substitute(
            selected: sqlite3.Connection,
            statement: str,
            parameters: tuple[Any, ...] = (),
            deadline: store.Deadline | None = None,
        ) -> sqlite3.Cursor:
            nonlocal injected
            if operation == "prepare" and statement.startswith("INSERT INTO completion_intents"):
                injected = True
                changed = list(parameters)
                changed[4] = "aborted"
                parameters = tuple(changed)
            elif operation == "finalize" and statement.startswith("INSERT INTO completion_receipts"):
                injected = True
                receipt = json.loads(parameters[3])
                receipt["completed_at"] = "2099-01-01T00:00:00Z"
                receipt["digest"] = planning.digest({key: value for key, value in receipt.items() if key != "digest"})
                parameters = (*parameters[:3], store.canonical(receipt))
            elif operation == "abort" and statement.startswith("UPDATE completion_intents SET phase='aborted'"):
                injected = True
                statement = statement.replace("SET phase='aborted'", "SET phase='prepared'")
            return original(selected, statement, parameters, deadline)

        with monkeypatch.context() as patched:
            patched.setattr(completion_store, "_execute", substitute)
            with pytest.raises(store.StoreError):
                if operation == "prepare":
                    journal.prepare(intent(retained), **proof(retained))
                elif operation == "finalize":
                    journal.finalize(identity, completion._receipt(retained["intent"], case.binding))
                else:
                    journal.abort(identity)
        assert injected
        with pytest.raises(store.StoreError, match="invalidated"):
            journal.get(identity)
        assert case.ledger.read_bytes() == before
        with completion_store.validated(connection) as fresh:
            assert fresh.get(identity)["phase"] == ("aborted" if operation == "prepare" else "prepared")
    assert_auditable(case)


def test_registration_cannot_seal_a_different_but_structurally_valid_watermark(
    case: FaultScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sqlite3.connect
    injected = False

    class SubstitutedWatermark(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            nonlocal injected
            if sql.startswith("INSERT INTO watermarks"):
                injected = True
                parameters = (parameters[0], parameters[1] + 1, parameters[2])
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=SubstitutedWatermark)

    before = case.ledger.read_bytes()
    state = case.state_path.read_bytes()
    with monkeypatch.context() as patched:
        patched.setattr(sqlite3, "connect", connect)
        with pytest.raises(store.StoreError):
            store.put(case.ledger, case.binding)
    assert injected
    assert case.ledger.read_bytes() == before and case.state_path.read_bytes() == state
    assert_auditable(case)


@pytest.mark.parametrize("failure", ["raise_after_index_writes", "unexpected_extra_write"])
def test_failed_or_contaminated_index_maintenance_rolls_back_source_and_index_together(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    case, retained = prepared
    identity = retained["intent"]["id"]
    before = case.ledger.read_bytes()
    original = execution_index.maintain

    def interrupted(
        connection: sqlite3.Connection,
        checked: execution_index.Checked,
        *,
        changes: Any,
        deadline: store.Deadline | None = None,
    ) -> tuple[int, execution_index.Checked]:
        count, result = original(connection, checked, changes=changes, deadline=deadline)
        assert count > 0 and connection.in_transaction
        if failure == "raise_after_index_writes":
            raise store.StoreError("injected after baseline and verdict writes")
        connection.execute("UPDATE completion_intents SET phase=phase WHERE id=?", (identity,))
        return count, result

    with database(case) as connection, completion_store.validated(connection) as journal:
        with monkeypatch.context() as patched:
            patched.setattr(execution_index, "maintain", interrupted)
            with pytest.raises(store.StoreError):
                journal.finalize(identity, completion._receipt(retained["intent"], case.binding))
        with pytest.raises(store.StoreError, match="invalidated"):
            journal.abort(identity)
        with completion_store.validated(connection) as fresh:
            assert fresh.get(identity) == retained
    assert case.ledger.read_bytes() == before
    assert_auditable(case)

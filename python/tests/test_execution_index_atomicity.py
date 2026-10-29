"""Index-writing faults cannot publish unlisted source changes or fabricated semantic facts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from exacly import completion, completion_store, history_maintenance, planning
from exacly import execution_history as history
from exacly import execution_store as store
from test_completion_faults import FaultScenario
from test_completion_store import database
from test_execution_history_reuse_faults import case as case
from test_execution_history_reuse_faults import prepared as prepared

Doc = dict[str, Any]


def change_unlisted_binding(connection: sqlite3.Connection, identity: str) -> sqlite3.Cursor:
    text = connection.execute("SELECT record_json FROM bindings WHERE id=?", (identity,)).fetchone()[0]
    changed = json.loads(text)
    changed["registered_at"] = "2099-01-01T00:00:00Z"
    return connection.execute("UPDATE bindings SET record_json=? WHERE id=?", (store.canonical(changed), identity))


def assert_unchanged_auditable(case: FaultScenario, before: bytes, state: bytes) -> None:
    assert case.ledger.read_bytes() == before
    assert case.state_path.read_bytes() == state
    audited = history_maintenance.audit(case.ledger)
    assert audited["status"] == "passed", audited
    assert audited["validation"]["semantics"]["reused"] == 0


@pytest.mark.parametrize("substitute", [False, True])
def test_public_registration_rolls_back_source_changes_inside_the_index_writer(
    case: FaultScenario, monkeypatch: pytest.MonkeyPatch, substitute: bool
) -> None:
    before, state = case.ledger.read_bytes(), case.state_path.read_bytes()
    execute = history._execute
    injected = False

    def late_source_write(
        connection: sqlite3.Connection,
        statement: str,
        parameters: tuple[Any, ...] = (),
        deadline: store.Deadline | None = None,
    ) -> sqlite3.Cursor:
        nonlocal injected
        if not injected and statement.startswith("UPDATE history_profile SET roster_digest"):
            injected = True
            if substitute:
                # Replace one intended DML statement with another: an exact write count alone
                # cannot detect that the source changed instead of the index metadata.
                return change_unlisted_binding(connection, case.binding["id"])
            cursor = execute(connection, statement, parameters, deadline)
            change_unlisted_binding(connection, case.binding["id"])
            return cursor
        return execute(connection, statement, parameters, deadline)

    with monkeypatch.context() as patched:
        patched.setattr(history, "_execute", late_source_write)
        with pytest.raises(store.StoreError):
            store.put(case.ledger, case.binding)
    assert injected
    assert_unchanged_auditable(case, before, state)


@pytest.mark.parametrize("operation", ["abort", "finalize"])
def test_journal_rolls_back_unlisted_source_writes_during_terminal_index_publication(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    case, retained = prepared
    identity = retained["intent"]["id"]
    before, state = case.ledger.read_bytes(), case.state_path.read_bytes()
    execute = history._execute
    injected = False

    def late_source_write(
        connection: sqlite3.Connection,
        statement: str,
        parameters: tuple[Any, ...] = (),
        deadline: store.Deadline | None = None,
    ) -> sqlite3.Cursor:
        nonlocal injected
        cursor = execute(connection, statement, parameters, deadline)
        if not injected and statement.startswith("INSERT INTO history_semantics"):
            injected = True
            change_unlisted_binding(connection, case.binding["id"])
        return cursor

    with database(case) as connection:
        with completion_store.validated(connection) as journal:
            with monkeypatch.context() as patched:
                patched.setattr(history, "_execute", late_source_write)
                with pytest.raises(store.StoreError):
                    if operation == "abort":
                        journal.abort(identity)
                    else:
                        journal.finalize(identity, completion._receipt(retained["intent"], case.binding))
            assert injected
            with pytest.raises(store.StoreError, match="invalidated"):
                journal.get(identity)
        with completion_store.validated(connection) as fresh:
            assert fresh.get(identity) == retained
            assert fresh.completed_for_binding(case.binding["id"]) is None
    assert_unchanged_auditable(case, before, state)


def test_same_count_self_consistent_wrong_verdict_rolls_back_before_it_can_be_reused(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, retained = prepared
    identity = retained["intent"]["id"]
    before, state = case.ledger.read_bytes(), case.state_path.read_bytes()
    execute = history._execute
    injected = False

    def wrong_verdict(
        connection: sqlite3.Connection,
        statement: str,
        parameters: tuple[Any, ...] = (),
        deadline: store.Deadline | None = None,
    ) -> sqlite3.Cursor:
        nonlocal injected
        if not injected and statement.startswith("INSERT INTO history_semantics"):
            injected = True
            changed = json.loads(parameters[3])
            changed["before_snapshot"]["serial"] += 1
            parameters = (*parameters[:3], store.canonical(changed), planning.digest(changed))
        return execute(connection, statement, parameters, deadline)

    with database(case) as connection:
        with completion_store.validated(connection) as journal:
            with monkeypatch.context() as patched:
                patched.setattr(history, "_execute", wrong_verdict)
                with pytest.raises(store.StoreError):
                    journal.abort(identity)
            assert injected
            with pytest.raises(store.StoreError, match="invalidated"):
                journal.get(identity)
        with completion_store.validated(connection) as fresh:
            assert fresh.get(identity) == retained
    assert_unchanged_auditable(case, before, state)

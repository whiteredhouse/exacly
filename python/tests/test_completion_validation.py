"""History fixtures and regressions for operation-scoped completion proof validation.

The synthetic history builder is development-only: each copied aborted attempt retains the real
admission proof and transition from a normal completion, with a distinct, recomputed intent identity.
It avoids timing the quadratic cost of generating benchmark history through repeated public writes.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from exacly import admission, completion, planning, state_writer
from exacly import execution_store as store
from test_completion_faults import FaultScenario, NoNetwork, prepare_case

Doc = dict[str, Any]


def history(tmp_path: Path, count: int, *, prepared: bool = False) -> tuple[FaultScenario, str]:
    """Seed a fully validated real-proof history; only the selected operation can be unresolved."""
    if count < 1:
        raise ValueError("history requires at least one retained intent")
    case = prepare_case(tmp_path)
    if prepared:
        publish = state_writer.publish_locked

        def interrupted(*args: Any, **kwargs: Any) -> None:
            publish(*args, **kwargs)
            raise OSError("benchmark interruption after state publication")

        with patch.object(state_writer, "publish_locked", interrupted):
            try:
                case.complete()
            except completion.CompletionInterruptedError:
                pass
            else:
                raise AssertionError("fixture did not retain a prepared completion")
    else:
        assert case.complete()["status"] == "completed"
    with sqlite3.connect(case.ledger) as connection:
        row = connection.execute("SELECT * FROM completion_intents").fetchone()
        assert row is not None
        identity = str(row[0])
        metadata = json.loads(row[5])
        for _ in range(count - 1):
            copied = list(row)
            changed = copy.deepcopy(metadata)
            copied[0] = changed["id"] = uuid.uuid4().hex
            copied[4] = "aborted"
            changed["digest"] = planning.digest({key: value for key, value in changed.items() if key != "digest"})
            copied[5] = store.canonical(changed)
            placeholders = ",".join("?" for _ in copied)
            connection.execute(f"INSERT INTO completion_intents VALUES ({placeholders})", copied)
    # Setup must not let the benchmark accidentally measure invalid or unchecked proof.
    with store.locked(case.ledger, write=False) as (path, _), store.database(path, create=False) as (connection, _):
        store.validate(connection)
    return case, identity


def operation(case: FaultScenario, identity: str, name: str) -> Callable[[], Doc]:
    """Use public offline paths with a generous wait budget for development measurements."""
    if name == "show":
        return lambda: completion.show(case.ledger, identity, lock_timeout=120)
    if name == "replay":
        return lambda: case.complete(source=NoNetwork(), lock_timeout=120)
    if name == "recover":
        return lambda: completion.recover(case.ledger, identity, state_path=case.state_path, lock_timeout=120)
    raise ValueError(f"unknown completion operation: {name}")


@pytest.mark.parametrize("name,passes", [("show", 1), ("replay", 1), ("recover", 3)])
def test_each_history_is_rederived_once_per_protected_database_snapshot(tmp_path: Path, name: str, passes: int) -> None:
    count = 4
    case, identity = history(tmp_path, count, prepared=name == "recover")
    with patch.object(admission, "observe_report", wraps=admission.observe_report) as observed:
        result = operation(case, identity, name)()
    assert result["status"] == "completed"
    # Recovery reads once, rechecks after acquiring its write locks, and validates its committed
    # transition. Read-only paths need only their single protected snapshot, regardless of helpers.
    assert observed.call_count == count * passes


@pytest.mark.parametrize("name", ["show", "replay", "recover"])
def test_next_operation_rejects_corruption_of_an_unrelated_aborted_attempt(tmp_path: Path, name: str) -> None:
    case, identity = history(tmp_path, 4, prepared=name == "recover")
    invoke = operation(case, identity, name)
    assert invoke()["status"] == "completed"
    with sqlite3.connect(case.ledger) as connection:
        unrelated = connection.execute("SELECT id, report FROM completion_intents WHERE phase='aborted'").fetchone()
        assert unrelated is not None and unrelated[0] != identity
        connection.execute("UPDATE completion_intents SET report=? WHERE id=?", (unrelated[1] + b"\n", unrelated[0]))
    ledger_bytes = case.ledger.read_bytes()
    state_bytes = case.state_path.read_bytes()
    with pytest.raises(store.StoreError):
        invoke()
    assert case.ledger.read_bytes() == ledger_bytes
    assert case.state_path.read_bytes() == state_bytes

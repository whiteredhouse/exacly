"""Scalability fixtures must preserve real proof associations and one possible state history."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from exacly import admission, executions
from exacly import execution_store as store
from ledger_history_fixtures import build

ROOT = Path(__file__).resolve().parents[2]
Doc = dict[str, Any]


@pytest.mark.parametrize("backlog", [False, True])
def test_completed_history_has_distinct_proofs_and_continuous_shared_state(tmp_path: Path, backlog: bool) -> None:
    case = build(tmp_path, 3, depth=2, cases=6, requirements=3, backlog=backlog)
    with sqlite3.connect(case.ledger) as connection:
        rows = connection.execute(
            "SELECT intent_json,before_state,after_state,archive,report,admission_json,model_json "
            "FROM completion_intents"
        ).fetchall()
        bindings = connection.execute("SELECT record_json FROM bindings").fetchall()
        assert len(bindings) == 6
        assert len(connection.execute("SELECT * FROM active").fetchall()) == 3
        assert len(connection.execute("SELECT * FROM completion_receipts").fetchall()) == 3
        for (text,) in bindings:
            executions.validate_record(json.loads(text))
    ordered = sorted(rows, key=lambda row: json.loads(row[0])["state"]["after_serial"])
    assert len({admission.sha256(row[3]) for row in ordered}) == 3
    assert len({admission.sha256(row[4]) for row in ordered}) == 3
    assert len({json.loads(row[0])["binding_id"] for row in ordered}) == 3
    assert len({json.loads(row[5])["origin"]["artifact_id"] for row in ordered}) == 3
    assert len({json.loads(row[5])["origin"]["run_id"] for row in ordered}) == 3
    previous: Doc | None = None
    previous_bytes: bytes | None = None
    for index, (metadata, before_bytes, after_bytes, archive, report, evidence, model) in enumerate(ordered):
        before, after = json.loads(before_bytes), json.loads(after_bytes)
        assert before["lineage"] == after["lineage"]
        assert after["serial"] == before["serial"] + 1
        assert len(before.get("pending", [])) == (3 - index if backlog else 1)
        assert len(after.get("pending", [])) == (2 - index if backlog else 0)
        assert len(list(ET.fromstring(report).iter("testcase"))) == 6
        assert admission.unpack(archive)[1] == report
        assert json.loads(evidence)["origin"]["artifact_name"].endswith(f"-{100000 + index}-1")
        model_doc = next(iter(json.loads(model).values()))
        assert sum(record["type"] == "Requirement" for record in model_doc["records"]) == 3
        assert json.loads(metadata)["state"]["path"] == str(case.state)
        if previous is not None:
            if backlog:
                assert before == previous
                assert before_bytes == previous_bytes
            else:
                assert before["serial"] == previous["serial"] + 1
                # Queuing the next task is the only intervening publication.
                assert {
                    key: value for key, value in before.items() if key not in {"serial", "written_at", "pending"}
                } == {key: value for key, value in previous.items() if key not in {"serial", "written_at", "pending"}}
        previous = after
        previous_bytes = after_bytes
    assert previous == json.loads(case.state.read_bytes())


def test_open_binding_history_preserves_each_supersession_chain(tmp_path: Path) -> None:
    case = build(tmp_path, 3, depth=3, bindings_only=True)
    with store.locked(case.ledger, write=False) as (path, _), store.database(path, create=False) as (connection, _):
        records, active, _ = store.validate(connection)
        assert len(records) == 9 and len(active) == 3
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        for identity in active.values():
            visited = []
            while identity is not None:
                visited.append(identity)
                identity = records[identity]["supersedes"]
            assert len(visited) == 3 and len(set(visited)) == 3


def test_full_validation_rejects_a_changed_terminal_proof(tmp_path: Path) -> None:
    case = build(tmp_path, 2)
    with sqlite3.connect(case.ledger) as connection:
        identity, content = connection.execute("SELECT id,report FROM completion_intents ORDER BY id").fetchone()
        connection.execute("UPDATE completion_intents SET report=? WHERE id=?", (content + b"\n", identity))
    original = case.ledger.read_bytes()
    with (
        store.locked(case.ledger, write=False) as (path, _),
        store.database(path, create=False) as (connection, _),
        pytest.raises(store.StoreError),
    ):
        store.validate(connection)
    assert case.ledger.read_bytes() == original


def test_profile_cli_reports_deterministic_work_and_exact_read_hash_bytes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks/ledger_history.py"), "--counts", "2", "--supersession-depth", "2"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / path) for path in ("python/src", "python/tests"))},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    measured = document["results"][0]
    assert measured["tasks"] == measured["completed_proofs"] == 2
    assert measured["bindings"] == 4
    assert measured["current_binding_group_comparisons"] == 8
    assert measured["repetitions"][0]["proof_rederivations"] == 2
    assert measured["repetitions"][0]["calls_by_component"]["binding_decode_seconds"] == 4
    sizes = measured["sizes"]
    assert measured["repetitions"][0]["read_and_hash_bytes"] == (
        sizes["binding_json_bytes"] + sizes["retained_payload_bytes"] + sizes["receipt_json_bytes"]
    )
    assert len(measured["repetitions"][0]["read_and_hash_digest"]) == 64


@pytest.mark.parametrize(
    "changes",
    [{"tasks": 0}, {"depth": 0}, {"cases": 0}, {"requirements": 0}, {"cases": 2, "requirements": 3}],
)
def test_invalid_fixture_dimensions_are_rejected_before_generation(tmp_path: Path, changes: Doc) -> None:
    with pytest.raises(ValueError):
        build(tmp_path, **{"tasks": 1, **changes})

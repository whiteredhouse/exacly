"""The protected journal retains exact proof and rejects inconsistent completion history."""

from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3
import stat
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from exacly import admission, completion, completion_store, planning, state_writer
from exacly import execution_store as store
from exacly.applicability import Target
from test_admission import Source, bundle
from test_completion_faults import FaultScenario, prepare_case

Doc = dict[str, Any]


@contextmanager
def database(case: FaultScenario) -> Iterator[sqlite3.Connection]:
    deadline = store.Deadline(2.0)
    with (
        store.locked(case.ledger, write=True, deadline=deadline) as (path, _),
        store.database(path, create=True, deadline=deadline) as (connection, _created),
    ):
        yield connection


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FaultScenario, Doc]:
    case = prepare_case(tmp_path)

    def unpublished(*args: Any, **kwargs: Any) -> None:
        raise OSError("stop after durable preparation")

    before = case.state_path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(state_writer, "publish_locked", unpublished)
        with pytest.raises(completion.CompletionInterruptedError):
            case.complete()
    assert case.state_path.read_bytes() == before
    with database(case) as connection:
        retained = completion_store.current(connection)
    assert retained is not None
    return case, retained


def intent(value: Doc) -> Doc:
    return {
        "id": uuid.uuid4().hex,
        "request": copy.deepcopy(value["intent"]["request"]),
        "prepared_at": value["intent"]["prepared_at"],
    }


def proof(value: Doc) -> Doc:
    return {
        key: copy.deepcopy(value[key])
        for key in (
            "before_state",
            "after_state",
            "archive",
            "report",
            "admission_receipt",
            "model_documents",
            "admitted_state",
        )
    }


def test_prepare_retains_exact_bytes_and_does_not_advance_the_watermark(prepared: tuple[FaultScenario, Doc]) -> None:
    case, retained = prepared
    metadata = retained["intent"]
    before = json.loads(retained["before_state"])
    assert retained["phase"] == "prepared" and retained["receipt"] is None
    assert retained["before_state"] == case.state_path.read_bytes()
    assert metadata["state"]["before_digest"] == admission.sha256(retained["before_state"])
    assert metadata["state"]["after_digest"] == admission.sha256(retained["after_state"])
    assert metadata["evidence"]["archive_digest"] == admission.sha256(retained["archive"])
    assert admission.unpack(retained["archive"])[1] == retained["report"]
    snapshot = case.ledger.read_bytes()
    with database(case) as connection:
        assert completion_store.get(connection, metadata["id"]) == retained
        assert completion_store.find_completed(connection, metadata["request_digest"]) is None
        assert completion_store.completed_for_binding(connection, case.binding["id"]) is None
        envelope = completion_store.envelope(connection, case.binding["id"])
    assert envelope["state_watermark"] == {"serial": before["serial"], "digest": planning.digest(before)}
    assert case.ledger.read_bytes() == snapshot


def test_finalize_is_immutable_replay_and_advances_the_state_watermark(prepared: tuple[FaultScenario, Doc]) -> None:
    case, retained = prepared
    metadata = retained["intent"]
    receipt = completion._receipt(metadata, case.binding)
    with database(case) as connection:
        assert completion_store.finalize(connection, metadata["id"], receipt) == receipt
    snapshot = case.ledger.read_bytes()
    with database(case) as connection:
        assert (
            completion_store.find_completed(connection, metadata["request_digest"], binding_id=case.binding["id"])
            == receipt
        )
        assert completion_store.completed_for_binding(connection, case.binding["id"]) == receipt
        assert completion_store.finalize(connection, metadata["id"], receipt) == receipt
        assert completion_store.current(connection) is None
        envelope = completion_store.envelope(connection, case.binding["id"])
        assert envelope["completion"]["phase"] == "committed"
        after = json.loads(retained["after_state"])
        assert envelope["state_watermark"] == {"serial": after["serial"], "digest": planning.digest(after)}
        different = copy.deepcopy(receipt)
        different["completed_at"] = "2099-01-01T00:00:00Z"
        different["digest"] = planning.digest({key: value for key, value in different.items() if key != "digest"})
        with pytest.raises(store.StoreError, match="immutable"):
            completion_store.finalize(connection, metadata["id"], different)
        with pytest.raises(store.StoreError, match="different request"):
            completion_store.find_completed(connection, "sha256:" + "f" * 64, binding_id=case.binding["id"])
        with pytest.raises(store.StoreError):
            completion_store.abort(connection, metadata["id"])
    assert case.ledger.read_bytes() == snapshot
    with pytest.raises(store.StoreError, match="completed pending"):
        store.put(case.ledger, case.binding)


def test_abort_retains_proof_and_allows_only_a_new_intent(prepared: tuple[FaultScenario, Doc]) -> None:
    case, retained = prepared
    identity = retained["intent"]["id"]
    with database(case) as connection:
        aborted = completion_store.abort(connection, identity)
        assert aborted == {**retained, "phase": "aborted"}
        assert completion_store.abort(connection, identity) == aborted
        assert completion_store.current(connection) is None
        assert completion_store.completed_for_binding(connection, case.binding["id"]) is None
        with pytest.raises(store.StoreError):
            completion_store.prepare(connection, {**intent(retained), "id": identity}, **proof(retained))
        replacement = completion_store.prepare(connection, intent(retained), **proof(retained))
        assert replacement["intent"]["id"] != identity
        assert replacement["intent"]["request_digest"] == retained["intent"]["request_digest"]
        assert completion_store.get(connection, identity)["phase"] == "aborted"


@pytest.mark.parametrize(
    "column",
    [
        "before_state",
        "after_state",
        "archive",
        "report",
        "intent_json",
        "admission_json",
        "model_json",
        "admitted_state_json",
    ],
)
def test_corrupt_retained_payload_is_rejected_on_every_store_read(
    prepared: tuple[FaultScenario, Doc], column: str
) -> None:
    case, retained = prepared
    with sqlite3.connect(case.ledger) as connection:
        value = connection.execute(f"SELECT {column} FROM completion_intents").fetchone()[0]
        changed = value + b"\n" if isinstance(value, bytes) else value + " "
        connection.execute(f"UPDATE completion_intents SET {column}=?", (changed,))
    before = case.ledger.read_bytes()
    with pytest.raises(store.StoreError):
        store.get(case.ledger, case.binding["id"])
    with database(case) as connection, pytest.raises(store.StoreError):
        completion_store.get(connection, retained["intent"]["id"])
    assert case.ledger.read_bytes() == before


@pytest.mark.parametrize("phase", ["committed", "aborted"])
def test_phase_and_receipt_consistency_is_checked(prepared: tuple[FaultScenario, Doc], phase: str) -> None:
    case, retained = prepared
    receipt = completion._receipt(retained["intent"], case.binding)
    with sqlite3.connect(case.ledger) as connection:
        if phase == "committed":
            connection.execute("UPDATE completion_intents SET phase='committed'")
        else:
            connection.execute("UPDATE completion_intents SET phase='aborted'")
            connection.execute(
                "INSERT INTO completion_receipts (binding_id,intent_id,request_digest,receipt_json) VALUES (?,?,?,?)",
                (
                    case.binding["id"],
                    retained["intent"]["id"],
                    retained["intent"]["request_digest"],
                    store.canonical(receipt),
                ),
            )
    before = case.ledger.read_bytes()
    with pytest.raises(store.StoreError):
        store.get(case.ledger, case.binding["id"])
    assert case.ledger.read_bytes() == before


@pytest.mark.parametrize("constant", ["STATE_LIMIT", "ARCHIVE_LIMIT", "JSON_LIMIT"])
def test_payload_bounds_are_checked_before_loading_proof(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    case, _ = prepared
    before = case.ledger.read_bytes()
    monkeypatch.setattr(completion_store, constant, 1)
    with pytest.raises(store.StoreError, match="bound"):
        store.get(case.ledger, case.binding["id"])
    assert case.ledger.read_bytes() == before


def test_an_invalid_before_state_cannot_be_pruned_into_a_valid_after_state(prepared: tuple[FaultScenario, Doc]) -> None:
    case, retained = prepared
    proposed = proof(retained)
    before = json.loads(proposed["before_state"])
    before["records"]["UNKNOWN"] = copy.deepcopy(before["records"]["REQ-1"])
    proposed["before_state"] = state_writer.serialize(before)
    snapshot = case.ledger.read_bytes()
    with database(case) as connection:
        completion_store.abort(connection, retained["intent"]["id"])
        snapshot = case.ledger.read_bytes()
        with pytest.raises(store.StoreError, match="before-state"):
            completion_store.prepare(connection, intent(retained), **proposed)
    assert case.ledger.read_bytes() == snapshot


@pytest.mark.parametrize("outcome", ["skipped", "failure"])
def test_retained_junit_cannot_contradict_a_positive_after_state(
    prepared: tuple[FaultScenario, Doc], outcome: str
) -> None:
    case, retained = prepared
    proposed = proof(retained)
    xml = ET.fromstring(proposed["report"])
    for case_element in xml.iter("testcase"):
        ET.SubElement(case_element, outcome)
    report = ET.tostring(xml, encoding="utf-8")
    manifest = admission.unpack(proposed["archive"])[0]
    manifest["report_digest"] = admission.sha256(report)
    archive = bundle(
        [("manifest.json", json.dumps(manifest).encode(), stat.S_IFREG), ("junit.xml", report, stat.S_IFREG)]
    )
    proposed.update(archive=archive, report=report)
    admitted = proposed["admission_receipt"]
    admitted["origin"].update(archive_digest=admission.sha256(archive), report_digest=admission.sha256(report))
    admitted["digest"] = planning.digest({key: value for key, value in admitted.items() if key != "digest"})
    with database(case) as connection:
        completion_store.abort(connection, retained["intent"]["id"])
        snapshot = case.ledger.read_bytes()
        with pytest.raises(store.StoreError):
            completion_store.prepare(connection, intent(retained), **proposed)
    assert case.ledger.read_bytes() == snapshot


def test_report_from_another_real_admission_cannot_support_the_passing_after_state(
    prepared: tuple[FaultScenario, Doc],
) -> None:
    case, retained = prepared
    request = retained["intent"]["request"]
    source = Source(report=b'<testsuite><testcase file="unrelated.py" name="other" /></testsuite>')
    admitted = admission.admit(
        source,
        source.policy,
        Target(request["target"]),
        request["run_id"],
        request["attempt"],
        dt.date.fromisoformat(request["as_of"]),
    )
    assert admitted.receipt["status"] == "admitted" and admitted.state is not None
    assert admitted.state["records"]["REQ-1"]["assessments"][0]["state"] == "not_checked"
    proposed = proof(retained)
    proposed.update(
        archive=admitted.archive,
        report=admitted.report,
        admission_receipt=admitted.receipt,
        admitted_state=admitted.state,
    )
    with database(case) as connection:
        completion_store.abort(connection, retained["intent"]["id"])
        before = case.ledger.read_bytes()
        with pytest.raises(store.StoreError, match="exact admitted observer transition"):
            completion_store.prepare(connection, intent(retained), **proposed)
    assert case.ledger.read_bytes() == before


def test_unrelated_retained_evidence_cannot_change_in_the_after_state(prepared: tuple[FaultScenario, Doc]) -> None:
    case, retained = prepared
    proposed = proof(retained)
    after = json.loads(proposed["after_state"])
    after["records"]["REQ-1"]["evidence"][0]["claim"] = "unrelated history was rewritten"
    proposed["after_state"] = state_writer.serialize(after)
    with database(case) as connection:
        completion_store.abort(connection, retained["intent"]["id"])
        before = case.ledger.read_bytes()
        with pytest.raises(store.StoreError, match="exact admitted observer transition"):
            completion_store.prepare(connection, intent(retained), **proposed)
    assert case.ledger.read_bytes() == before


def test_admitted_state_must_match_its_retained_receipt_digest(prepared: tuple[FaultScenario, Doc]) -> None:
    case, retained = prepared
    proposed = proof(retained)
    proposed["admitted_state"]["serial"] += 1
    with database(case) as connection:
        completion_store.abort(connection, retained["intent"]["id"])
        before = case.ledger.read_bytes()
        with pytest.raises(store.StoreError, match="differs from its admission receipt"):
            completion_store.prepare(connection, intent(retained), **proposed)
    assert case.ledger.read_bytes() == before


def test_failed_finalize_transaction_leaves_the_original_prepared_intent(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, retained = prepared
    receipt = completion._receipt(retained["intent"], case.binding)
    original_connect = sqlite3.connect

    class FailedWatermark(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith("UPDATE watermarks"):
                raise sqlite3.OperationalError("injected failure after receipt and phase writes")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=FailedWatermark)

    before = case.ledger.read_bytes()
    monkeypatch.setattr(sqlite3, "connect", connect)
    with database(case) as connection:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            completion_store.finalize(connection, retained["intent"]["id"], receipt)
        assert completion_store.get(connection, retained["intent"]["id"]) == retained
        assert completion_store.find_completed(connection, retained["intent"]["request_digest"]) is None
    assert case.ledger.read_bytes() == before
    assert set(path.name for path in case.ledger.parent.iterdir()) == {case.ledger.name}


def test_v2_writes_use_extra_synchronization_at_each_completion_commit(
    prepared: tuple[FaultScenario, Doc], monkeypatch: pytest.MonkeyPatch
) -> None:
    case, retained = prepared
    original_connect = sqlite3.connect
    levels: list[int] = []

    class ObservedCommit(sqlite3.Connection):
        def commit(self) -> None:
            levels.append(self.execute("PRAGMA synchronous").fetchone()[0])
            super().commit()

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original_connect(*args, **kwargs, factory=ObservedCommit)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with database(case) as connection:
        completion_store.abort(connection, retained["intent"]["id"])
        new = completion_store.prepare(connection, intent(retained), **proof(retained))
        completion_store.finalize(connection, new["intent"]["id"], completion._receipt(new["intent"], case.binding))
    assert levels == [3, 3, 3], "EXTRA includes directory synchronization after DELETE journal unlink"


def test_v1_reads_work_but_completion_requires_an_explicit_copy_upgrade(prepared: tuple[FaultScenario, Doc]) -> None:
    case, _ = prepared
    source = case.ledger.parent.parent.parent / "original-operator" / "ledger.sqlite3"
    before = source.read_bytes()
    assert store.get(source, case.binding["id"])["binding"] == case.binding
    with store.database(source, create=True) as (connection, _), pytest.raises(store.StoreError, match="upgraded v2"):
        completion_store.require_v2(connection)
    assert source.read_bytes() == before
    with sqlite3.connect(source) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    with sqlite3.connect(case.ledger) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)

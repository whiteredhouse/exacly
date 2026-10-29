"""Operational indexing preserves completion's real crash and concurrency contracts."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
from pathlib import Path
from typing import Any

import pytest
import test_completion_faults as faults
from exacly import completion, execution_maintenance, history_maintenance


@pytest.fixture(autouse=True)
def operational_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the existing independent state/publication contracts against explicit v4 copies."""
    original = faults.prepare_case

    def prepare(tmp_path: Path, **options: Any) -> faults.FaultScenario:
        case = original(tmp_path, **options)
        destination = tmp_path / "operational-operator"
        report = history_maintenance.activate(case.ledger, destination)
        assert report["status"] == "activated", report
        case.ledger = destination / "operational" / "ledger.sqlite3"
        return case

    monkeypatch.setattr(faults, "prepare_case", prepare)


def test_prepared_process_death_requires_recovery_and_fresh_admission(tmp_path: Path) -> None:
    faults.test_killed_after_preparation_aborts_without_publication_then_requires_fresh_admission(tmp_path)


@pytest.mark.parametrize("phase", ["after_replace", "before_finalize"])
def test_published_process_death_recovers_exactly_once(tmp_path: Path, phase: str) -> None:
    faults.test_killed_after_publication_recovers_the_original_intent_without_reapplying(tmp_path, phase)


def test_finalized_process_death_replays_the_original_receipt_offline(tmp_path: Path) -> None:
    faults.test_killed_after_final_receipt_replays_before_http_or_pending_matching(tmp_path)


def test_divergent_state_preserves_the_prepared_conflict(tmp_path: Path) -> None:
    faults.test_recovery_of_a_third_state_preserves_the_conflict_and_prepared_intent(tmp_path)


def test_live_prepared_writer_has_a_bounded_lock_wait(tmp_path: Path) -> None:
    faults.test_completion_lock_timeout_is_bounded_while_a_prepared_writer_is_alive(tmp_path)


def test_concurrent_writers_share_one_receipt_and_state_advance(tmp_path: Path) -> None:
    faults.test_concurrent_completion_callers_share_one_receipt_and_one_state_advance(tmp_path)


def test_supersession_during_admission_blocks_old_candidate_publication(tmp_path: Path) -> None:
    faults.test_supersession_during_admission_prevents_publication_of_the_old_binding(tmp_path)


def test_concurrent_observation_is_preserved_by_the_completion_merge(tmp_path: Path) -> None:
    faults.test_refresh_during_admission_is_retained_by_the_completion_merge(tmp_path)


def test_killed_index_and_receipt_transaction_recovers_a_coherent_copy(tmp_path: Path) -> None:
    case = faults.prepare_case(tmp_path, large_receipt=True)
    before = json.loads(case.state_path.read_bytes())
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=faults.interrupted_receipt_commit, args=(case, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30), "finalization did not reach its uncommitted transaction boundary"
        message = parent.recv()
        assert message[0] == "ready", message
        identity = message[1]
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        parent.close()

    published = case.state_path.read_bytes()
    after = json.loads(published)
    assert after["serial"] == before["serial"] + 1
    faults.remaining_work(after, before)
    source = case.ledger
    journal = Path(str(source) + "-journal")
    assert journal.read_bytes().startswith(bytes.fromhex("d9d505f920a163d7"))
    residue = {item.name: item.read_bytes() for item in source.parent.iterdir() if item.is_file()}
    with pytest.raises(ValueError, match=r"journal|sidecars"):
        completion.show(source, identity)
    assert execution_maintenance.inspect(source)["status"] == "recovery_required"

    destination = tmp_path / "recovered-operator"
    report = execution_maintenance.recover(source, destination)
    assert report["status"] == "recovered" and report["store"]["user_version"] == 4, report
    assert (destination / "raw/ledger.sqlite3").read_bytes() == residue[source.name]
    assert (destination / "raw/ledger.sqlite3-journal").read_bytes() == residue[journal.name]
    case.ledger = destination / "recovered/ledger.sqlite3"
    assert completion.show(case.ledger, identity)["status"] == "prepared"
    result = case.recover(identity)
    assert result["status"] == "completed", result
    assert case.state_path.read_bytes() == published
    assert case.source.calls == [] and case.source.downloads == []
    assert case.complete(source=faults.NoNetwork())["receipt"] == result["receipt"]
    assert case.recover(identity)["receipt"] == result["receipt"]
    audited = history_maintenance.audit(case.ledger)
    assert audited["status"] == "passed", audited
    assert audited["validation"]["semantics"]["reused"] == 0
    assert {item.name: item.read_bytes() for item in source.parent.iterdir() if item.is_file()} == residue

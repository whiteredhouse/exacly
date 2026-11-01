"""Real process ownership, journal serialization and withdrawal under controlled provider effects."""

from __future__ import annotations

import copy
import datetime as dt
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from delivery_engine_fixtures import CAPS, Case, make_case, read, write
from exacly import delivery_automation as contracts
from exacly import delivery_runtime, delivery_store, executor_runtime
from exacly import execution_store as store
from test_delivery_contracts import NOW, pin

Doc = dict[str, Any]


def wait_until(predicate: Callable[[], bool], *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("controlled process did not reach its expected boundary")
        time.sleep(0.01)


def launch(case: Case, result_path: Path) -> int:
    child = os.fork()
    if child == 0:
        try:
            write(result_path, case.apply())
        except BaseException:
            os._exit(91)
        os._exit(0)
    return child


def reaped(child: int, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while True:
        waited, status = os.waitpid(child, os.WNOHANG)
        if waited:
            assert status == 0
            return
        if time.monotonic() >= deadline:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
            pytest.fail("controlled coordinator exceeded test cleanup bound")
        time.sleep(0.01)


def operation(case: Case) -> Doc:
    return dict(delivery_store.show(case.journal, case.journal_id, case.request["operation_key"])["operation"])


def test_concurrent_coordinators_reserve_once_and_never_send_a_second_effect(tmp_path: Path) -> None:
    case = make_case(tmp_path, mode="wait")
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    first = launch(case, first_path)
    wait_until(lambda: bool(read(case.remote).get("started")))
    second = launch(case, second_path)
    reaped(second)
    assert read(second_path)["status"] == "uncertain"
    assert read(second_path)["historical"] is True
    case.remote.with_suffix(".release").touch()
    reaped(first)
    assert read(first_path)["status"] == "applied"
    assert read(case.remote)["writes"] == 1
    assert case.apply()["receipt"] == read(first_path)["receipt"]
    history = delivery_store.show(case.journal, case.journal_id)
    assert history["reserved"]["operations"] == 1
    assert [item["method"] for item in operation(case)["calls"]] == ["inspect", "invoke"]


def test_effective_revocation_waits_for_the_whole_admitted_compound_call(tmp_path: Path) -> None:
    caps = copy.deepcopy(CAPS)
    caps["invoke"]["wall_time_ms"] = 5000
    case = make_case(tmp_path, mode="wait", caps=caps)
    child = launch(case, tmp_path / "result.json")
    wait_until(lambda: bool(read(case.remote).get("started")))
    with pytest.raises(store.LockTimeoutError):
        delivery_store.revoke(case.journal, case.journal_id, reason="operator withdraws future work", lock_timeout=0.03)
    assert delivery_store.show(case.journal, case.journal_id)["revoked"] is False
    case.remote.with_suffix(".release").touch()
    reaped(child)
    revoked = delivery_store.revoke(case.journal, case.journal_id, reason="operator withdraws future work")
    assert revoked["revoked"] is True
    before = read(case.remote)
    # A genuinely new exact operation remains denied after the withdrawal linearization point.
    case.request["operation_key"] = contracts.digest("a later logical delivery item")
    case.approval.update(
        operation_key=case.request["operation_key"],
        request_digest=contracts.digest(case.request),
        approval_id="later independently reviewed change",
    )
    with pytest.raises(ValueError, match="revoked"):
        case.apply()
    assert read(case.remote) == before
    repeated = delivery_store.revoke(case.journal, case.journal_id, reason="this must not replace original history")
    assert repeated["revocation"] == revoked["revocation"]


def test_parent_death_keeps_the_watchdog_fence_and_recovery_does_not_resend(tmp_path: Path) -> None:
    caps = copy.deepcopy(CAPS)
    caps["invoke"]["wall_time_ms"] = 5000
    case = make_case(tmp_path, mode="hang", caps=caps)
    child = launch(case, tmp_path / "dead-parent.json")
    wait_until(lambda: bool(read(case.remote).get("started")))
    saved = operation(case)
    call = saved["calls"][-1]
    watcher = call["owner"]["process"]
    assert executor_runtime.alive(watcher)
    watcher_fd, parent_fd = os.pidfd_open(watcher["pid"]), os.pidfd_open(child)
    try:
        # Pause the actual watchdog to expose inheritance deterministically, then kill its parent.
        signal.pidfd_send_signal(watcher_fd, signal.SIGSTOP)
        signal.pidfd_send_signal(parent_fd, signal.SIGKILL)
        waited, status = os.waitpid(child, 0)
        assert waited == child and os.WIFSIGNALED(status)
        with pytest.raises(store.LockTimeoutError):
            delivery_store.revoke(case.journal, case.journal_id, reason="withdraw while owner lives", lock_timeout=0.03)
        assert delivery_store.show(case.journal, case.journal_id)["revoked"] is False
    finally:
        signal.pidfd_send_signal(watcher_fd, signal.SIGCONT)
        os.close(watcher_fd)
        os.close(parent_fd)
    wait_until(lambda: not executor_runtime.alive(watcher))
    history = delivery_store.show(case.journal, case.journal_id)
    assert history["reserved"]["operations"] == 1
    assert history["reserved"]["wall_time_ms"] == 8000
    observed = case.discover()
    assert observed["status"] == "uncertain"
    assert read(case.remote)["writes"] == 0
    assert operation(case)["calls"][-2]["cleanup"] is True
    assert case.apply()["status"] == "uncertain"
    assert read(case.remote)["writes"] == 0


def test_foreign_cleanup_receipt_cannot_prove_owned_processes_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path, mode="after")
    intended = case.engine.preview(case.request, case.payload, approval=case.approval, pin=pin(case.approval))[
        "packet"
    ]["intent"]
    invocation_id = contracts.digest({"operation": intended["id"], "ordinal": 1, "method": "invoke"})
    original_cleanup = delivery_runtime.retain_cleanup

    def omit_cleanup(metadata: Doc, call_id: str, cleaned: bool) -> None:
        if call_id != invocation_id:
            original_cleanup(metadata, call_id, cleaned)

    monkeypatch.setattr(delivery_runtime, "retain_cleanup", omit_cleanup)
    assert case.apply()["status"] == "uncertain"
    saved = operation(case)
    call = saved["calls"][-1]
    assert call["cleanup"] is None
    with delivery_store.opened(case.journal, case.journal_id) as journal:
        metadata = copy.deepcopy(journal.metadata)
    path = delivery_runtime._cleanup_path(metadata, call["id"])
    changed = {
        "schema_version": "0.1.0",
        "kind": "delivery-runtime-cleanup",
        "journal_id": case.journal_id,
        "call_id": call["id"],
        "owner": copy.deepcopy(call["owner"]["process"]),
        "cleaned": True,
    }
    changed["owner"]["start_time"] += 1
    path.write_text(store.canonical(changed))
    path.chmod(0o600)
    before = read(case.remote)
    with pytest.raises(ValueError, match="another owner"):
        case.discover()
    assert operation(case)["calls"][-1]["cleanup"] is None
    assert read(case.remote) == before
    assert read(case.remote)["writes"] == 1


def test_recovery_approval_must_not_have_any_provider_write_capacity(tmp_path: Path) -> None:
    from test_delivery_engine import recovery_approval

    case = make_case(tmp_path, mode="after")
    assert case.apply()["status"] == "uncertain"
    approved = recovery_approval(case)
    approved["read_limits"]["write_calls"] = 1
    before = read(case.remote)
    with pytest.raises(ValueError):
        case.engine.reconcile(case.request["operation_key"], approval=approved, pin=pin(approved))
    assert read(case.remote) == before


def test_actual_native_deadline_cannot_outlive_the_current_approval_window(tmp_path: Path) -> None:
    from exacly.delivery_engine import DeliveryEngine

    caps = copy.deepcopy(CAPS)
    caps["invoke"]["wall_time_ms"] = 5000
    case = make_case(tmp_path, mode="delayed", caps=caps)
    started = time.monotonic()
    case.approval["expires_at"] = (NOW + dt.timedelta(seconds=3.5)).isoformat()
    case.engine = DeliveryEngine(
        case.journal,
        journal_id=case.journal_id,
        adapter_factory=lambda: case.adapter,
        clock=lambda: NOW + dt.timedelta(seconds=time.monotonic() - started),
    )
    result = case.apply()
    assert result["status"] == "uncertain", result
    assert read(case.remote).get("started") is True
    assert read(case.remote)["writes"] == 0
    history = delivery_store.show(case.journal, case.journal_id)
    assert history["reserved"]["wall_time_ms"] == 8000  # Full original slots stay consumed.
    time.sleep(0.1)
    assert read(case.remote)["writes"] == 0

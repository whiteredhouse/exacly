"""Actual coordinator calls through independent ports with opaque, non-PID handles.

The controlled worker never executes candidate code. These tests prove composition and lifecycle
semantics, while existing real Bubblewrap cases remain the evidence for physical confinement.
"""

from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from coordinator_fixtures import (
    CALLBACK_FAULTS,
    ControlledWorker,
    ReferenceOwnership,
    ReferenceStorage,
    invalid_callbacks,
    reference_bundle,
)
from dispatch_fixtures import Scenario
from dispatch_fixtures import dispatch_scenario as dispatch_scenario
from exacly import executor, executor_runtime, executor_store, isolation, operation_locks, state_writer
from exacly.executor_interfaces import CoordinatorBackends, Scope
from executor_fixtures import ExecutorScenario, make_executor_scenario

Doc = dict[str, Any]


@dataclass
class InjectedCase:
    item: ExecutorScenario
    backends: CoordinatorBackends
    storage: ReferenceStorage
    ownership: ReferenceOwnership
    worker: ControlledWorker

    def preview(self, **options: Any) -> Doc:
        return executor.preview(
            self.item.documents, self.item.state_path, self.item.request, backends=self.backends, **options
        )

    def apply(self, authorization: Doc | None = None, **options: Any) -> Doc:
        return executor.apply(
            self.item.journal,
            self.item.ledger,
            self.item.documents,
            self.item.state_path,
            self.item.request,
            backends=self.backends,
            authorization=authorization if authorization is not None else self.preview()["authorization_template"],
            **options,
        )


@pytest.fixture
def injected_case(dispatch_scenario: Scenario, tmp_path: Path) -> InjectedCase:
    root = tmp_path / "unused-reviewed-runtime"
    root.mkdir(mode=0o700)
    # The controlled worker supplies no isolation evidence. No command uses this placeholder.
    runtime = isolation.Runtime(root, "sha256:" + "1" * 64, "/usr/bin/python3")
    item = make_executor_scenario(dispatch_scenario, runtime, tmp_path)
    scope = Scope(item.journal, item.ledger, item.state_path)
    bundle, storage, ownership, worker = reference_bundle(scope, item.policy["storage"]["journal_id"])
    return InjectedCase(item, bundle, storage, ownership, worker)


def test_coordinator_uses_all_injected_ports_without_local_attempt_or_process_calls(
    injected_case: InjectedCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = injected_case
    item = case.item
    original = item.authoritative_bytes(), item.journal.read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("the injected coordinator reached a local attempt/process implementation")

    monkeypatch.setattr(executor_store, "opened", forbidden)
    monkeypatch.setattr(executor_runtime, "run", forbidden)
    monkeypatch.setattr(executor_runtime, "validate_identity", forbidden)
    monkeypatch.setattr(operation_locks, "directories", forbidden)
    preview = case.preview()
    assert preview["status"] == "ready" and case.worker.calls == [] and case.storage.records == {}
    result = case.apply(preview["authorization_template"])
    assert result["status"] == "succeeded", result
    assert result["receipt"]["build"]["id"].startswith("sha256:")
    assert case.worker.calls == ["supervise", "reconcile"]
    assert any(coordinated for _, coordinated in case.storage.opens)
    assert (item.authoritative_bytes(), item.journal.read_bytes()) == original
    history = executor.show(item.journal, result["execution_id"], backends=case.backends)["history"]
    events = history["attempts"][0]["events"]
    assert set(events[0]["data"]["owner"]) == {"provider", "claim"}
    assert [set(event["data"]["identity"]) for event in events if event["data"]["kind"] in ("worker", "namespace")] == [
        {"provider", "job"},
        {"provider", "job"},
    ]
    assert result["claims"]["evidence_admitted"] is False
    assert result["claims"]["task_completion_authorized"] is False


def test_opaque_receipt_replay_needs_no_current_model_or_retained_output(injected_case: InjectedCase) -> None:
    case = injected_case
    authorization = case.preview()["authorization_template"]
    first = case.apply(authorization)
    assert first["status"] == "succeeded", first
    shutil.rmtree(first["receipt"]["output"])
    case.item.documents.clear()
    snapshot = state_writer.read(case.item.state_path)
    assert snapshot.document is not None
    changed = copy.deepcopy(snapshot.document)
    changed["serial"] += 1
    changed["config_revision"] = "d" * 40
    state_writer.publish(snapshot, changed)
    calls = list(case.worker.calls)
    report = case.apply(authorization)
    assert report["receipt"] == first["receipt"]
    assert case.worker.calls == calls
    assert (
        executor.recover(case.item.journal, first["execution_id"], backends=case.backends)["receipt"]
        == first["receipt"]
    )


def test_fresh_attempt_authorization_is_invalidated_by_actual_current_state(injected_case: InjectedCase) -> None:
    case = injected_case
    authorization = case.preview()["authorization_template"]
    snapshot = state_writer.read(case.item.state_path)
    assert snapshot.document is not None
    changed = copy.deepcopy(snapshot.document)
    changed["serial"] += 1
    changed["x-unrelated-observation"] = "must be retained"
    state_writer.publish(snapshot, changed)
    expected = case.item.authoritative_bytes()
    with pytest.raises(ValueError, match="authorization"):
        case.apply(authorization)
    assert case.worker.calls == [] and case.storage.records == {}
    assert case.item.authoritative_bytes() == expected


def test_policy_change_cannot_turn_a_consumed_attempt_into_a_new_budget(injected_case: InjectedCase) -> None:
    case = injected_case
    case.worker.interrupt = True
    first = case.apply()
    assert first["status"] == "interrupted", first
    before = copy.deepcopy(case.storage.records)
    changed = copy.deepcopy(case.item.policy)
    changed["max_attempts"] = 10
    changed_request = replace(case.item.request, executor_policy=changed)
    with pytest.raises(ValueError, match="retained contract"):
        executor.preview(case.item.documents, case.item.state_path, changed_request, backends=case.backends)
    assert case.storage.records == before
    assert case.worker.calls == ["supervise", "reconcile"]


def test_unverified_termination_blocks_replacement_until_model_independent_recovery(
    injected_case: InjectedCase,
) -> None:
    case = injected_case
    authorization = case.preview()["authorization_template"]
    case.worker.interrupt = True
    case.worker.cleanup_available = False
    interrupted = case.apply(authorization)
    assert interrupted["status"] == "interrupted" and interrupted["receipt"] is None, interrupted
    assert case.worker.running
    calls = list(case.worker.calls)
    replay = case.apply(authorization)
    assert replay["receipt"] is None and case.worker.calls == calls
    assert case.preview()["authorization_template"] is None
    case.item.documents.clear()
    case.worker.cleanup_available = True
    recovered = executor.recover(case.item.journal, interrupted["execution_id"], backends=case.backends)
    assert recovered["status"] == "interrupted" and recovered["receipt"]["attempt"] == 1, recovered
    assert not case.worker.running
    assert case.apply(authorization)["receipt"] == recovered["receipt"]
    retained = case.storage.records[interrupted["execution_id"]]
    assert len(retained["attempts"]) == 1 and retained["intent"]["max_attempts"] == 2


def test_live_cancellation_uses_injected_ownership_and_preserves_pending_work(injected_case: InjectedCase) -> None:
    case = injected_case
    expected = case.item.authoritative_bytes()
    identity = case.preview()["execution_id"]
    observed: list[Doc] = []

    def cancel() -> None:
        observed.append(executor.cancel(case.item.journal, identity, backends=case.backends))

    case.worker.after_worker = cancel
    report = case.apply()
    assert observed[0]["status"] == "cancellation_requested"
    assert report["status"] == "cancelled" and report["receipt"]["build"] is None, report
    assert not case.worker.running and case.item.authoritative_bytes() == expected


def test_revoked_invocation_cannot_commit_and_a_new_owner_must_reconcile(injected_case: InjectedCase) -> None:
    case = injected_case
    identity = case.preview()["execution_id"]

    def revoke() -> None:
        lease = next(iter(case.ownership.held.values()))
        case.ownership.revoke(lease)

    case.worker.after_worker = revoke
    report = case.apply()
    assert report["status"] == "interrupted" and report["receipt"] is None, report
    assert case.worker.running
    assert case.storage.records[identity]["phase"] == "running"
    recovered = executor.recover(case.item.journal, identity, backends=case.backends)
    assert recovered["status"] == "interrupted" and recovered["receipt"] is not None, recovered
    assert not case.worker.running


def test_recheck_before_boundary_release_preserves_a_newer_actual_state(injected_case: InjectedCase) -> None:
    case = injected_case
    expected: list[tuple[bytes, bytes]] = []

    def advance() -> None:
        snapshot = state_writer.read(case.item.state_path)
        assert snapshot.document is not None
        state = copy.deepcopy(snapshot.document)
        state["serial"] += 1
        state["x-new-observation"] = "retain this"
        state_writer.publish(snapshot, state)
        expected.append(case.item.authoritative_bytes())

    case.worker.after_worker = advance
    result = case.apply()
    assert result["status"] == "interrupted" and result["receipt"]["build"] is None, result
    assert case.item.authoritative_bytes() == expected[0]
    assert not list(case.item.output_root.rglob("report.json"))


@pytest.mark.parametrize("operation", ["preview", "apply", "show", "cancel", "recover", "recover-journal"])
def test_distributed_scope_is_rejected_before_any_backend_effect(injected_case: InjectedCase, operation: str) -> None:
    case = injected_case
    options: dict[str, Any] = {"backends": case.backends, "coordination_scope": "distributed"}
    with pytest.raises(ValueError, match="distributed"):
        if operation == "preview":
            executor.preview(case.item.documents, case.item.state_path, case.item.request, **options)
        elif operation == "apply":
            executor.apply(
                case.item.journal,
                case.item.ledger,
                case.item.documents,
                case.item.state_path,
                case.item.request,
                authorization={},
                **options,
            )
        elif operation == "recover-journal":
            executor.recover_journal(case.item.journal, authorize=True, **options)
        else:
            command = {"show": executor.show, "cancel": executor.cancel, "recover": executor.recover}[operation]
            command(case.item.journal, "sha256:" + "1" * 64, **options)
    assert case.storage.opens == [] and case.worker.calls == [] and case.storage.records == {}


@pytest.mark.parametrize(
    "field,value", [("scope", "distributed"), ("protocol_version", "99"), ("contract_adapter", "remote-provider-v1")]
)
def test_unsupported_backend_contract_is_rejected_before_storage(
    injected_case: InjectedCase,
    field: str,
    value: str,
) -> None:
    case = injected_case
    changes: dict[str, Any] = {field: value}
    invalid = replace(case.backends, **changes)
    with pytest.raises(ValueError):
        executor.preview(case.item.documents, case.item.state_path, case.item.request, backends=invalid)
    assert case.storage.opens == [] and case.worker.calls == []


@pytest.mark.parametrize("component", ["ownership", "workers", "storage"])
def test_components_from_unrelated_ownership_domains_cannot_be_assembled(
    injected_case: InjectedCase,
    component: str,
) -> None:
    case = injected_case
    unrelated, _, _, _ = reference_bundle(
        Scope(case.item.journal, case.item.ledger, case.item.state_path), case.item.policy["storage"]["journal_id"]
    )
    invalid = replace(case.backends, **{component: getattr(unrelated, component)})
    with pytest.raises(ValueError, match="domain"):
        executor.preview(case.item.documents, case.item.state_path, case.item.request, backends=invalid)
    assert case.storage.opens == [] and case.worker.calls == []


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "missing_boundary",
        "wrong_handle",
        "not_terminated",
        "reason",
        "outcome",
        "nonstring",
        "missing_outcome",
    ],
)
def test_malformed_backend_outcome_cannot_commit_a_poisoned_terminal_receipt(
    injected_case: InjectedCase,
    damage: str,
) -> None:
    case = injected_case
    authorization = case.preview()["authorization_template"]
    identity = authorization["execution_id"]

    def corrupt(result: Doc) -> Doc:
        result = copy.deepcopy(result)
        if damage == "missing":
            result["termination"] = {}
        elif damage == "missing_boundary":
            result["termination"]["namespace"] = None
        elif damage == "wrong_handle":
            result["termination"]["worker"]["identity"]["job"] = "another-job"
        elif damage == "not_terminated":
            result["termination"]["namespace"]["terminated"] = False
        elif damage == "reason":
            result["termination"]["worker"]["reason"] = "lease expired, assumed stopped"
        elif damage == "nonstring":
            result["status"] = True
        elif damage == "missing_outcome":
            result.pop("status")
        else:
            result["status"] = "invented-outcome"
        return result

    case.worker.mutate_result = corrupt
    try:
        report = case.apply(authorization)
    except ValueError:
        pass
    else:
        assert report["receipt"] is None
    retained = case.storage.records[identity]
    assert retained["phase"] == "running"
    assert not any(event["data"].get("kind") == "receipt" for event in retained["attempts"][0]["events"])
    shown = executor.show(case.item.journal, identity, backends=case.backends)
    assert shown["receipt"] is None
    case.worker.mutate_result = None
    recovered = executor.recover(case.item.journal, identity, backends=case.backends)
    assert recovered["status"] == "succeeded", recovered
    assert case.apply(authorization)["receipt"] == recovered["receipt"]


def test_candidate_reported_build_identity_cannot_replace_host_derived_artifact_identity(
    injected_case: InjectedCase,
) -> None:
    case = injected_case
    claimed = "sha256:" + "f" * 64

    def add_claim(result: Doc) -> Doc:
        return {**result, "build": {"id": claimed}, "verification_established": True}

    case.worker.mutate_result = add_claim
    report = case.apply()
    assert report["status"] == "succeeded", report
    assert report["receipt"]["build"]["id"] != claimed
    assert report["claims"]["verification_established"] is False
    assert report["receipt"]["claims"]["task_completion_authorized"] is False


@pytest.mark.parametrize("status", ["candidate_failed", "timed_out", "output_limit", "cancelled"])
def test_a_known_unsuccessful_worker_outcome_survives_a_complete_retained_bundle(
    injected_case: InjectedCase,
    status: str,
) -> None:
    case = injected_case
    case.worker.mutate_result = lambda result: {**result, "status": status}
    report = case.apply()
    assert report["status"] == ("failed" if status == "candidate_failed" else status), report
    assert report["receipt"]["build"] is None
    assert Path(report["receipt"]["output"], "report.json").is_file()
    assert case.preview()["authorization_template"]["attempt"] == 2


@pytest.mark.parametrize("fault", CALLBACK_FAULTS)
@pytest.mark.parametrize("finish", ["recover", "cancel"])
def test_rejected_callbacks_leave_readable_history_and_recoverable_consumed_attempts(
    injected_case: InjectedCase,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    finish: str,
) -> None:
    case = injected_case
    item = case.item
    original = item.authoritative_bytes()
    authorization = case.preview()["authorization_template"]
    identity = authorization["execution_id"]
    case.worker.cleanup_available = False

    def faulty(*args: Any, on_worker: Any, on_boundary: Any, **kwargs: Any) -> None:
        invalid_callbacks(
            fault,
            {"provider": "semantic-test-worker", "job": "retained-worker"},
            {"provider": "semantic-test-worker", "job": "retained-boundary"},
            on_worker=on_worker,
            on_boundary=on_boundary,
        )

    monkeypatch.setattr(case.worker, "supervise", faulty)
    interrupted = case.apply(authorization)
    assert interrupted["status"] == "interrupted" and interrupted["receipt"] is None, interrupted
    shown = executor.show(item.journal, identity, backends=case.backends)
    expected = {
        "worker_identity": [],
        "boundary_identity": ["worker"],
        "boundary_first": [],
        "worker_twice": ["worker"],
        "boundary_twice": ["worker", "namespace"],
    }[fault]
    events = shown["history"]["attempts"][0]["events"]
    assert [event["data"]["kind"] for event in events[1:]] == expected
    assert shown["receipt"] is None and case.preview()["authorization_template"] is None
    assert case.apply(authorization)["receipt"] is None
    assert item.authoritative_bytes() == original

    # Cleanup must work from retained facts even after the current model is unavailable.
    documents = copy.deepcopy(item.documents)
    item.documents.clear()
    case.worker.cleanup_available = True
    command = executor.recover if finish == "recover" else executor.cancel
    recovered = command(item.journal, identity, backends=case.backends)
    assert recovered["status"] == ("interrupted" if finish == "recover" else "cancelled"), recovered
    assert recovered["receipt"]["attempt"] == 1 and recovered["receipt"]["build"] is None
    assert case.apply(authorization)["receipt"] == recovered["receipt"]
    assert executor.show(item.journal, identity, backends=case.backends)["receipt"] == recovered["receipt"]
    assert item.authoritative_bytes() == original
    item.documents.update(documents)
    assert case.preview()["authorization_template"]["attempt"] == 2


def test_mutating_callback_identity_cannot_change_its_validated_retained_snapshot(
    injected_case: InjectedCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = injected_case
    worker = {"provider": "semantic-test-worker", "job": "original-worker"}
    original = dict(worker)
    validate = case.worker.validate_identity

    def mutate_source(value: Any) -> Doc:
        checked = validate(value)
        if value == original:
            worker["job"] = "changed-by-callback-provider"
        return checked

    def supervise(*args: Any, on_worker: Any, **kwargs: Any) -> None:
        on_worker(worker)
        raise ValueError("controlled supervisor stop")

    monkeypatch.setattr(case.worker, "validate_identity", mutate_source)
    monkeypatch.setattr(case.worker, "supervise", supervise)
    report = case.apply()
    assert report["status"] == "interrupted", report
    assert worker != original
    events = report["history"]["attempts"][0]["events"]
    assert events[1]["data"]["identity"] == original
    assert report["receipt"]["termination"][0]["identity"] == original

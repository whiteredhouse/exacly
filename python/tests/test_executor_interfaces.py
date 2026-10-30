"""Shared semantic tests run against SQLite and an independent in-memory reference.

Only the real SQLite/crash and Bubblewrap suites establish persistence and physical isolation.
"""

from __future__ import annotations

import copy
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from coordinator_fixtures import ControlledWorker, ReferenceOwnership, ReferenceStorage, work_intent
from exacly import executor_runtime, executor_store
from exacly.executor_interfaces import (
    AttemptReference,
    AttemptStorage,
    InvocationOwnership,
    Scope,
    StoreIdentity,
    WorkerHandle,
    WorkerLifecycle,
)
from exacly.executor_local import local_backends

Doc = dict[str, Any]


@dataclass
class StoreCase:
    storage: AttemptStorage
    ownership: InvocationOwnership
    scope: Scope
    outputs: Path
    identity: StoreIdentity
    workers: WorkerLifecycle

    def reference(self, intent: Doc, number: int = 1) -> AttemptReference:
        return AttemptReference(
            intent["id"],
            number,
            self.outputs / intent["id"].removeprefix("sha256:") / f"attempt-{number}",
            self.identity,
        )


@pytest.fixture(params=["sqlite", "memory"])
def store_case(request: pytest.FixtureRequest, tmp_path: Path) -> StoreCase:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    outputs = tmp_path / "outputs"
    outputs.mkdir(mode=0o700)
    scope = Scope(private / "attempts.sqlite3", private / "bindings.sqlite3")
    if request.param == "sqlite":
        initialized = executor_store.initialize(
            scope.journal, ledger=scope.ledger or private / "unused", max_concurrency=1
        )
        backend = local_backends()
        return StoreCase(
            backend.storage,
            backend.ownership,
            scope,
            outputs,
            StoreIdentity(str(scope.journal), initialized["journal_id"]),
            backend.workers,
        )
    ownership = ReferenceOwnership()
    storage = ReferenceStorage(scope, journal_id="1" * 32, ownership=ownership)
    return StoreCase(storage, ownership, scope, outputs, storage.identity, ControlledWorker(ownership))


def reservation(reference: AttemptReference) -> Doc:
    return {"authorization": {"output": str(reference.output)}, "approved_attempt": reference.number}


def test_sessions_expose_stable_identity_and_detached_reads(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent()
    reference = case.reference(proposed)
    with case.ownership.claim(reference, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            assert session.ledger == str(case.scope.ledger)
            assert session.max_concurrency == 1
            identity = session.journal_id
            assert session.get(proposed["id"]) is None
            prepared = session.prepare(proposed, lease=lease)
            assert session.prepare(proposed, lease=lease) == prepared
            prepared["intent"]["contract"]["approved"] = "caller mutation"
            retained = session.get(proposed["id"])
            assert retained is not None and retained["intent"] == proposed
            proposed["contract"]["approved"] = "another caller mutation"
            assert session.get(proposed["id"]) == retained
    with case.storage.open(case.scope, write=False, lock_timeout=1) as session:
        assert session.journal_id == identity
        assert session.get(reference.execution_id) == retained


@pytest.mark.parametrize("field", ["contract", "max_attempts"])
def test_changed_contract_cannot_reset_retained_limits(store_case: StoreCase, field: str) -> None:
    case = store_case
    proposed = work_intent()
    with case.ownership.claim(case.reference(proposed), create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            original = session.prepare(proposed, lease=lease)
            changed = copy.deepcopy(proposed)
            changed[field] = {"unreviewed": True} if field == "contract" else 10
            with pytest.raises(ValueError):
                session.prepare(changed, lease=lease)
            assert session.get(proposed["id"]) == original


def test_unresolved_attempt_keeps_capacity_until_a_terminal_transition(store_case: StoreCase) -> None:
    case = store_case
    first, second = work_intent(), work_intent("second")
    first_ref, second_ref = case.reference(first), case.reference(second)
    with case.ownership.claim(first_ref, create=True) as one, case.ownership.claim(second_ref, create=True) as two:
        assert one is not None and two is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            session.prepare(first, lease=one)
            session.prepare(second, lease=two)
            before = session.reserve(first["id"], reservation(first_ref), lease=one)
            with pytest.raises(ValueError):
                session.reserve(first["id"], reservation(first_ref), lease=one)
            with pytest.raises(ValueError, match="concurrency"):
                session.reserve(second["id"], reservation(second_ref), lease=two)
            assert session.get(first["id"]) == before
            session.append(first["id"], 1, "interrupted", {"terminated": True}, lease=one)
            assert session.reserve(second["id"], reservation(second_ref), lease=two)["phase"] == "reserved"


def test_failed_attempts_are_consumed_and_retries_preserve_the_prior_history(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent(attempts=2)
    previous: Doc | None = None
    for number in (1, 2):
        reference = case.reference(proposed, number)
        with case.ownership.claim(reference, create=True) as lease:
            assert lease is not None
            with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
                if number == 1:
                    session.prepare(proposed, lease=lease)
                reserved = session.reserve(proposed["id"], reservation(reference), lease=lease)
                if previous is not None:
                    assert reserved["attempts"][0] == previous["attempts"][0]
                    assert reserved["attempts"][1]["events"][0]["previous"] == previous["digest"]
                previous = session.append(proposed["id"], number, "failed", {"terminated": True}, lease=lease)
    with case.ownership.claim(case.reference(proposed, 3), create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            with pytest.raises(ValueError, match="budget"):
                session.reserve(proposed["id"], reservation(case.reference(proposed, 3)), lease=lease)
            assert session.get(proposed["id"]) == previous


def test_success_and_its_terminal_receipt_are_immutable(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent()
    reference = case.reference(proposed)
    with case.ownership.claim(reference, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            session.prepare(proposed, lease=lease)
            session.reserve(proposed["id"], reservation(reference), lease=lease)
            receipt = {"outcome": "succeeded", "termination": "verified"}
            final = session.append(proposed["id"], 1, "succeeded", receipt, lease=lease)
            assert session.append(proposed["id"], 1, "succeeded", receipt, lease=lease) == final
            with pytest.raises(ValueError):
                session.append(proposed["id"], 1, "succeeded", {"different": True}, lease=lease)
            assert session.get(proposed["id"]) == final
    next_ref = case.reference(proposed, 2)
    with case.ownership.claim(next_ref, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session, pytest.raises(ValueError):
            session.reserve(proposed["id"], reservation(next_ref), lease=lease)


def test_read_only_session_rejects_mutation(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent()
    with case.ownership.claim(case.reference(proposed), create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=False, lock_timeout=1) as session, pytest.raises(ValueError):
            session.prepare(proposed, lease=lease)
    with case.storage.open(case.scope, write=False, lock_timeout=1) as session:
        assert session.get(proposed["id"]) is None


def test_unowned_mutation_is_limited_to_exact_current_cancellation(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent()
    reference = case.reference(proposed)
    with case.ownership.claim(reference, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            session.prepare(proposed, lease=lease)
            session.reserve(proposed["id"], reservation(reference), lease=lease)
            requested = session.append(proposed["id"], 1, "reserved", {"kind": "cancellation_requested"})
            assert requested["attempts"][0]["events"][-1]["data"] == {"kind": "cancellation_requested"}
            with pytest.raises(ValueError):
                session.append(proposed["id"], 1, "failed", {"kind": "receipt"})
            with pytest.raises(ValueError):
                session.append(proposed["id"], 1, "reserved", {"kind": "cancellation_requested", "extra": True})
            assert session.get(proposed["id"]) == requested


@pytest.mark.parametrize("different", ["work", "attempt", "output"])
def test_ownership_reference_must_match_the_exact_storage_effect(store_case: StoreCase, different: str) -> None:
    case = store_case
    proposed = work_intent()
    reference = case.reference(proposed)
    with case.ownership.claim(reference, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            session.prepare(proposed, lease=lease)
    bad = AttemptReference(
        work_intent("different")["id"] if different == "work" else reference.execution_id,
        2 if different == "attempt" else 1,
        reference.output.with_name("different-output") if different == "output" else reference.output,
        reference.store,
    )
    with case.ownership.claim(bad, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            before = session.get(proposed["id"])
            with pytest.raises(ValueError):
                session.reserve(proposed["id"], reservation(reference), lease=lease)
            assert session.get(proposed["id"]) == before


def test_closed_claim_cannot_write_even_after_a_new_claim_is_acquired(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent()
    reference = case.reference(proposed)
    with case.ownership.claim(reference, create=True) as original:
        assert original is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            session.prepare(proposed, lease=original)
    with case.ownership.claim(reference, create=False) as current:
        assert current is not None and current.token is not original.token
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            with pytest.raises(ValueError):
                session.reserve(proposed["id"], reservation(reference), lease=original)
            assert session.reserve(proposed["id"], reservation(reference), lease=current)["phase"] == "reserved"


def test_competing_sessions_cannot_reserve_more_than_the_fixed_capacity(store_case: StoreCase) -> None:
    case = store_case
    proposed = [work_intent("one"), work_intent("two")]
    for value in proposed:
        with case.ownership.claim(case.reference(value), create=True) as lease:
            assert lease is not None
            with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
                session.prepare(value, lease=lease)
    barrier = threading.Barrier(2)

    def compete(value: Doc) -> str:
        reference = case.reference(value)
        with case.ownership.claim(reference, create=False) as lease:
            assert lease is not None
            barrier.wait(timeout=5)
            with case.storage.open(case.scope, write=True, lock_timeout=2) as session:
                try:
                    session.reserve(value["id"], reservation(reference), lease=lease)
                except ValueError as error:
                    assert "concurrency" in str(error)
                    return "blocked"
                return "reserved"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(compete, proposed)) == ["blocked", "reserved"]
    with case.storage.open(case.scope, write=False, lock_timeout=1) as session:
        retained = [session.get(value["id"]) for value in proposed]
    assert sum(bool(value and value["attempts"]) for value in retained) == 1


@dataclass
class ForgedLease:
    """Copying exposed token fields does not produce a backend-issued capability."""

    domain: object
    token: object
    reference: AttemptReference
    owner_identity: Doc

    def assert_current(self) -> None:
        return None


def test_a_forged_claim_cannot_write_by_copying_an_issued_token(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent()
    with case.ownership.claim(case.reference(proposed), create=True) as lease:
        assert lease is not None
        forged = ForgedLease(lease.domain, lease.token, lease.reference, lease.owner_identity)
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            with pytest.raises(ValueError):
                session.prepare(proposed, lease=forged)
            assert session.get(proposed["id"]) is None
            session.prepare(proposed, lease=lease)
            with pytest.raises(ValueError):
                session.reserve(proposed["id"], reservation(lease.reference), lease=forged)
            retained = session.get(proposed["id"])
            assert retained is not None and retained["attempts"] == []


@pytest.mark.parametrize("different", ["locator", "instance"])
def test_claims_are_bound_to_the_initialized_store_not_only_work_and_output(
    store_case: StoreCase,
    different: str,
) -> None:
    case = store_case
    proposed = work_intent()
    reference = case.reference(proposed)
    with case.ownership.claim(reference, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            before = session.prepare(proposed, lease=lease)
    other = StoreIdentity(
        str(case.scope.journal.with_name("another.sqlite3")) if different == "locator" else reference.store.locator,
        "2" * 32 if different == "instance" else reference.store.instance_id,
    )
    forged_scope = AttemptReference(reference.execution_id, reference.number, reference.output, other)
    with pytest.raises(ValueError), case.ownership.claim(forged_scope, create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            session.reserve(proposed["id"], reservation(reference), lease=lease)
    with case.storage.open(case.scope, write=False, lock_timeout=1) as session:
        assert session.get(proposed["id"]) == before


def test_a_session_cannot_be_used_after_its_checked_view_is_closed(store_case: StoreCase) -> None:
    case = store_case
    proposed = work_intent()
    with case.ownership.claim(case.reference(proposed), create=True) as lease:
        assert lease is not None
        with case.storage.open(case.scope, write=True, lock_timeout=1) as session:
            session.prepare(proposed, lease=lease)
        with pytest.raises(ValueError):
            session.get(proposed["id"])
        with pytest.raises(ValueError):
            session.prepare(proposed, lease=lease)


def test_busy_invocation_cannot_be_claimed_by_a_second_owner(store_case: StoreCase) -> None:
    reference = store_case.reference(work_intent())
    with store_case.ownership.claim(reference, create=True) as first:
        assert first is not None
        with store_case.ownership.claim(reference, create=False) as second:
            assert second is None
        first.assert_current()


@pytest.fixture
def worker_target(store_case: StoreCase) -> Iterator[tuple[StoreCase, WorkerHandle]]:
    case = store_case
    reference = case.reference(work_intent())
    process: subprocess.Popen[bytes] | None = None
    if isinstance(case.workers, ControlledWorker):
        identity = {"provider": "semantic-test-worker", "job": "controlled-owned-job"}
        case.workers.running.add(identity["job"])
    else:
        process = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"])
        identity = executor_runtime.process_identity(process.pid)
    try:
        yield case, WorkerHandle(reference, identity)
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_worker_lifecycle_inspects_and_reconciles_the_exact_owned_target(
    worker_target: tuple[StoreCase, WorkerHandle],
) -> None:
    case, handle = worker_target
    assert case.workers.inspect(handle)
    with case.ownership.claim(handle.reference, create=True) as lease:
        assert lease is not None
        proofs = case.workers.reconcile([handle], lease=lease)
        assert len(proofs) == 1 and proofs[0]["terminated"] is True
        assert proofs[0]["identity"] == handle.identity
    assert not case.workers.inspect(handle)


def test_worker_lifecycle_rejects_all_mismatched_targets_before_stopping_any(
    worker_target: tuple[StoreCase, WorkerHandle],
) -> None:
    case, handle = worker_target
    wrong = AttemptReference(handle.reference.execution_id, 2, handle.reference.output, handle.reference.store)
    with case.ownership.claim(handle.reference, create=True) as lease:
        assert lease is not None
        with pytest.raises(ValueError):
            case.workers.reconcile([handle, WorkerHandle(wrong, handle.identity)], lease=lease)
        assert case.workers.inspect(handle)
        case.workers.reconcile([handle], lease=lease)


def test_worker_lifecycle_rejects_a_released_claim_before_termination(
    worker_target: tuple[StoreCase, WorkerHandle],
) -> None:
    case, handle = worker_target
    with case.ownership.claim(handle.reference, create=True) as stale:
        assert stale is not None
    with pytest.raises(ValueError):
        case.workers.reconcile([handle], lease=stale)
    assert case.workers.inspect(handle)
    with case.ownership.claim(handle.reference, create=False) as current:
        assert current is not None
        case.workers.reconcile([handle], lease=current)

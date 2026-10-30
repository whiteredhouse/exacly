"""Local port implementations retain storage and ownership guarantees at their boundary."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from exacly import execution_store, executor_runtime, executor_store, planning
from exacly.executor_interfaces import (
    AttemptReference,
    CoordinatorBackends,
    CoordinatorLockTimeoutError,
    InvocationLease,
    Scope,
    StoreIdentity,
    WorkerHandle,
    WorkerSpec,
)
from exacly.executor_local import LocalWorkerLifecycle, local_backends

Doc = dict[str, Any]


def intent(name: str = "first") -> Doc:
    identity = {"task": name, "adapter": "reviewed-local"}
    return {
        "schema_version": "0.1.0",
        "id": planning.digest(identity),
        "identity": identity,
        "contract": {"criteria": "reviewed"},
        "created_at": "2026-10-30T09:00:00Z",
        "max_attempts": 2,
    }


@pytest.fixture
def backend(tmp_path: Path) -> tuple[CoordinatorBackends, Scope, Path]:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    journal, ledger = private / "attempts.sqlite3", private / "bindings.sqlite3"
    executor_store.initialize(journal, ledger=ledger)
    output = tmp_path / "outputs"
    output.mkdir(mode=0o700)
    return local_backends(), Scope(journal, ledger), output


def reference(root: Path, scope: Scope, *, name: str = "first", number: int = 1) -> AttemptReference:
    identity = intent(name)["id"]
    with executor_store.opened(scope.journal, write=False) as journal:
        store = StoreIdentity(str(scope.journal), journal.journal_id)
    return AttemptReference(identity, number, root / identity.removeprefix("sha256:") / f"attempt-{number}", store)


def reservation(selected: AttemptReference) -> Doc:
    return {"kind": "reservation", "authorization": {"output": str(selected.output)}}


def test_factory_constructs_one_consistent_opaque_local_domain(
    backend: tuple[CoordinatorBackends, Scope, Path],
) -> None:
    components, scope, output = backend
    components.require_scope()
    assert components.storage.domain is components.ownership.domain is components.workers.domain
    with components.ownership.claim(reference(output, scope), create=True) as lease:
        assert lease is not None
        assert lease.domain is components.storage.domain
        assert lease.token is lease.token
        assert not hasattr(lease, "fileno")
        first = lease.owner_identity
        first["pid"] = 0
        assert lease.owner_identity["pid"] != 0


def test_mixed_components_are_rejected_before_any_ownership_effect(
    backend: tuple[CoordinatorBackends, Scope, Path],
) -> None:
    components, scope, output = backend
    mixed = replace(components, workers=LocalWorkerLifecycle())
    before = scope.journal.read_bytes()
    with pytest.raises(ValueError, match="domain"):
        mixed.require_scope()
    assert scope.journal.read_bytes() == before
    assert list(output.iterdir()) == []


def test_local_invocation_is_exclusive_and_closed_claims_cannot_be_reused(
    backend: tuple[CoordinatorBackends, Scope, Path],
) -> None:
    components, scope, output = backend
    selected = reference(output, scope)
    with components.ownership.claim(selected, create=True) as lease:
        assert lease is not None
        with components.ownership.claim(selected, create=False) as other:
            assert other is None
        with components.storage.open(scope, write=True, lock_timeout=1) as session:
            session.prepare(intent(), lease=lease)
    before = scope.journal.read_bytes()
    with (
        components.storage.open(scope, write=True, lock_timeout=1) as session,
        pytest.raises(ValueError, match="ownership"),
    ):
        session.reserve(selected.execution_id, reservation(selected), lease=lease)
    assert scope.journal.read_bytes() == before


def test_session_cannot_escape_its_locked_lifetime(backend: tuple[CoordinatorBackends, Scope, Path]) -> None:
    components, scope, _ = backend
    with components.storage.open(scope, write=False, lock_timeout=1) as session:
        assert session.get(intent()["id"]) is None
    with pytest.raises(ValueError, match="closed"):
        session.get(intent()["id"])
    with pytest.raises(ValueError, match="closed"):
        _ = session.journal_id


def test_read_only_session_rejects_owned_mutation(backend: tuple[CoordinatorBackends, Scope, Path]) -> None:
    components, scope, output = backend
    before = scope.journal.read_bytes()
    with (
        components.ownership.claim(reference(output, scope), create=True) as lease,
        components.storage.open(scope, write=False, lock_timeout=1) as session,
    ):
        assert lease is not None
        with pytest.raises(ValueError, match="read-only"):
            session.prepare(intent(), lease=lease)
    assert scope.journal.read_bytes() == before


class PretendedLease:
    """Copying public fields cannot mint a backend-issued ownership capability."""

    def __init__(self, original: InvocationLease) -> None:
        self.domain = original.domain
        self.reference = original.reference
        self.token = original.token
        self.owner_identity = original.owner_identity

    def assert_current(self) -> None:
        pass


@pytest.mark.parametrize("foreign", [False, True])
def test_same_domain_forgery_and_foreign_lease_are_rejected_before_writes(
    backend: tuple[CoordinatorBackends, Scope, Path], foreign: bool
) -> None:
    components, scope, output = backend
    issuer = local_backends() if foreign else components
    before = scope.journal.read_bytes()
    with issuer.ownership.claim(reference(output, scope), create=True) as issued:
        assert issued is not None
        supplied = issued if foreign else PretendedLease(issued)
        with (
            components.storage.open(scope, write=True, lock_timeout=1) as session,
            pytest.raises(ValueError, match="issued"),
        ):
            session.prepare(intent(), lease=supplied)
        with pytest.raises(ValueError, match="issued"):
            components.workers.reconcile([], lease=supplied)
    assert scope.journal.read_bytes() == before


@pytest.mark.parametrize("change", ["work", "number", "output"])
def test_reservation_is_bound_to_exact_claimed_work_attempt_and_output(
    backend: tuple[CoordinatorBackends, Scope, Path], change: str
) -> None:
    components, scope, output = backend
    selected = reference(output, scope)
    with components.ownership.claim(selected, create=True) as lease:
        assert lease is not None
        with components.storage.open(scope, write=True, lock_timeout=1) as session:
            session.prepare(intent(), lease=lease)
    wrong = (
        reference(output, scope, name="other")
        if change == "work"
        else replace(selected, number=2)
        if change == "number"
        else replace(selected, output=output / "wrong" / "attempt-1")
    )
    before = scope.journal.read_bytes()
    with (
        components.ownership.claim(wrong, create=True) as supplied,
        components.storage.open(scope, write=True, lock_timeout=1) as session,
    ):
        assert supplied is not None
        with pytest.raises(ValueError, match=r"another|differs"):
            session.reserve(selected.execution_id, reservation(selected), lease=supplied)
    assert scope.journal.read_bytes() == before


def test_replaced_owner_file_revokes_claim_before_storage_or_worker_effect(
    backend: tuple[CoordinatorBackends, Scope, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    components, scope, output = backend
    selected = reference(output, scope)
    with components.ownership.claim(selected, create=True) as lease:
        assert lease is not None
        path = selected.output.with_name("." + selected.output.name + ".owner")
        path.unlink()
        path.write_bytes(b"")
        path.chmod(0o600)
        before = scope.journal.read_bytes()
        with components.storage.open(scope, write=True, lock_timeout=1) as session, pytest.raises(ValueError):
            session.prepare(intent(), lease=lease)
        monkeypatch.setattr(executor_runtime, "terminate", lambda _: pytest.fail("revoked claim reached runtime"))
        with pytest.raises(ValueError):
            components.workers.reconcile([], lease=lease)
        assert scope.journal.read_bytes() == before


def test_same_bundle_cannot_reuse_a_claim_against_another_journal(
    backend: tuple[CoordinatorBackends, Scope, Path],
) -> None:
    components, scope, output = backend
    another = scope.journal.with_name("another.sqlite3")
    assert scope.ledger is not None
    executor_store.initialize(another, ledger=scope.ledger)
    second = replace(scope, journal=another)
    selected = reference(output, scope)
    before = another.read_bytes()
    with (
        components.ownership.claim(selected, create=True) as lease,
        components.storage.open(second, write=True, lock_timeout=1) as session,
    ):
        assert lease is not None
        assert session.identity.locator == str(another)
        assert session.identity.instance_id != lease.reference.store.instance_id
        with pytest.raises(ValueError, match="another store"):
            session.prepare(intent(), lease=lease)
    assert another.read_bytes() == before


def test_cancellation_is_the_only_unowned_mutation_and_repeated_request_is_idempotent(
    backend: tuple[CoordinatorBackends, Scope, Path],
) -> None:
    components, scope, output = backend
    selected = reference(output, scope)
    with (
        components.ownership.claim(selected, create=True) as lease,
        components.storage.open(scope, write=True, lock_timeout=1) as session,
    ):
        assert lease is not None
        session.prepare(intent(), lease=lease)
        session.reserve(selected.execution_id, reservation(selected), lease=lease)
        with pytest.raises(ValueError, match="only cancellation"):
            session.append(selected.execution_id, 1, "failed", {"kind": "receipt"})
        cancelled = session.append(selected.execution_id, 1, "reserved", {"kind": "cancellation_requested"})
        session.append(selected.execution_id, 1, "running", {"kind": "worker"}, lease=lease)
        repeated = session.append(selected.execution_id, 1, "reserved", {"kind": "cancellation_requested"})
        assert len(repeated["attempts"][0]["events"]) == len(cancelled["attempts"][0]["events"]) + 1
        assert (
            sum(event["data"] == {"kind": "cancellation_requested"} for event in repeated["attempts"][0]["events"]) == 1
        )


def test_coordinated_session_holds_binding_and_state_directories_in_same_operation(
    backend: tuple[CoordinatorBackends, Scope, Path], tmp_path: Path
) -> None:
    components, original, _ = backend
    binding = tmp_path / "bindings"
    state = tmp_path / "state"
    binding.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    scope = Scope(original.journal, original.ledger, state / "current.json")
    assert scope.ledger is not None
    with components.storage.open(scope, write=True, lock_timeout=1, coordinated=True):
        for path in (scope.ledger, state / "current.json"):
            with (
                pytest.raises(execution_store.LockTimeoutError),
                execution_store.locked(path, write=True, lock_timeout=0),
            ):
                pytest.fail("coordinated authority lock was not held")


def test_storage_translates_only_bounded_coordination_timeout(
    backend: tuple[CoordinatorBackends, Scope, Path],
) -> None:
    components, scope, _ = backend
    with (
        execution_store.locked(scope.journal, write=True),
        pytest.raises(CoordinatorLockTimeoutError),
        components.storage.open(scope, write=False, lock_timeout=0),
    ):
        pytest.fail("a busy store was opened")


def test_worker_port_delegates_gates_and_checks_claim_after_callback(
    backend: tuple[CoordinatorBackends, Scope, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    components, scope, output = backend
    selected = reference(output, scope)
    spec = WorkerSpec(
        {
            "schema_version": "0.1.0",
            "backend": "linux-bubblewrap-v1",
            "rootfs": "/reviewed/rootfs",
            "rootfs_digest": "sha256:" + "0" * 64,
            "python": "/usr/bin/python3",
            "bubblewrap": "/usr/bin/bwrap",
        },
        ("/usr/bin/python3", "-I", "-S", "/inputs/task.py"),
        {"task.py": b"reviewed bytes"},
        selected.output,
        5,
        selected,
    )
    events: list[str] = []
    saved = executor_runtime.process_identity()

    def run(*args: Any, **options: Any) -> Doc:
        assert args[1] == spec.argv and args[2] == spec.inputs and args[3] == spec.out
        assert not options["is_cancelled"]()
        options["on_worker"](saved)
        options["on_namespace"](saved)
        return {"status": "completed", "retained": True}

    monkeypatch.setattr(executor_runtime, "run", run)
    with components.ownership.claim(selected, create=True) as lease:
        assert lease is not None
        result = components.workers.supervise(
            spec,
            on_worker=lambda _: events.append("worker"),
            on_boundary=lambda _: events.append("boundary"),
            is_cancelled=lambda: False,
            lease=lease,
        )
        assert result["retained"] is True
        assert events == ["worker", "boundary"]
        with pytest.raises(ValueError, match="output differs"):
            components.workers.supervise(
                replace(spec, out=output / "other"),
                on_worker=lambda _: None,
                on_boundary=lambda _: None,
                is_cancelled=lambda: False,
                lease=lease,
            )


def test_worker_recovery_checks_every_reference_before_signalling_any_process(
    backend: tuple[CoordinatorBackends, Scope, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    components, scope, output = backend
    selected = reference(output, scope)
    identity = executor_runtime.process_identity()
    handles = [WorkerHandle(selected, identity), WorkerHandle(replace(selected, number=2), identity)]
    monkeypatch.setattr(executor_runtime, "terminate", lambda _: pytest.fail("invalid scope reached signalling"))
    with components.ownership.claim(selected, create=True) as lease:
        assert lease is not None
        with pytest.raises(ValueError, match="another invocation"):
            components.workers.reconcile(handles, lease=lease)


def test_mutating_returned_metadata_cannot_replace_owner_identity(
    backend: tuple[CoordinatorBackends, Scope, Path],
) -> None:
    components, scope, output = backend
    with components.ownership.claim(reference(output, scope), create=True) as lease:
        assert lease is not None
        owner = copy.deepcopy(lease.owner_identity)
        supplied = lease.owner_identity
        supplied.clear()
        assert lease.owner_identity == owner

"""Independent semantic reference backends for coordinator conformance tests.

The in-memory store and controlled worker are test implementations. They establish neither crash
persistence nor physical isolation or remote-provider support; those remain real local backend tests.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exacly import isolation, planning
from exacly.executor_interfaces import (
    AttemptReference,
    AttemptSession,
    CoordinatorBackends,
    CoordinatorLockTimeoutError,
    InvocationLease,
    Scope,
    StoreIdentity,
    WorkerHandle,
    WorkerSpec,
)

Doc = dict[str, Any]
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "output_invalid", "timed_out", "output_limit"}


def check(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def work_intent(name: str = "first", *, attempts: int = 2) -> Doc:
    identity = {"schema_version": "0.1.0", "task": name, "adapter": "semantic-test-adapter"}
    return {
        "schema_version": "0.1.0",
        "id": planning.digest(identity),
        "identity": identity,
        "contract": {"approved": name, "literal": "'; SELECT untrusted_text; --"},
        "created_at": "2026-10-30T09:00:00Z",
        "max_attempts": attempts,
    }


@dataclass
class ReferenceLease:
    manager: ReferenceOwnership
    reference: AttemptReference
    owner_identity: Doc
    token: object = field(default_factory=object)
    active: bool = True

    @property
    def domain(self) -> object:
        return self.manager.domain

    def assert_current(self) -> None:
        check(
            self.active
            and self.manager.held.get(self.reference) is self
            and self.manager.issued.get(self.token) is self,
            "ownership is no longer current",
        )


class ReferenceOwnership:
    """Exclusive invocation ownership with deliberately non-PID identities; no lease expiry."""

    def __init__(self, domain: object | None = None) -> None:
        self.domain = domain if domain is not None else object()
        self.known: set[AttemptReference] = set()
        self.held: dict[AttemptReference, ReferenceLease] = {}
        self.issued: dict[object, ReferenceLease] = {}
        self.serial = 0
        self.lock = threading.Lock()
        self.revoke_on_claim = False

    @contextmanager
    def claim(self, reference: AttemptReference, *, create: bool) -> Iterator[InvocationLease | None]:
        with self.lock:
            check(create or reference in self.known, "ownership facts are missing")
            self.known.add(reference)
            if reference in self.held:
                lease = None
            else:
                if create:
                    reference.output.parent.mkdir(mode=0o700, exist_ok=True)
                self.serial += 1
                lease = ReferenceLease(self, reference, {"provider": "semantic-test-owner", "claim": str(self.serial)})
                self.held[reference] = lease
                self.issued[lease.token] = lease
                if self.revoke_on_claim:
                    self.revoke(lease)
        try:
            yield lease
        finally:
            if lease is not None:
                with self.lock:
                    lease.active = False
                    self.issued.pop(lease.token, None)
                    if self.held.get(reference) is lease:
                        self.held.pop(reference)

    def revoke(self, lease: ReferenceLease) -> None:
        lease.active = False
        self.issued.pop(lease.token, None)
        if self.held.get(lease.reference) is lease:
            self.held.pop(lease.reference)

    def validate_identity(self, value: Any) -> Doc:
        check(
            isinstance(value, dict)
            and set(value) == {"provider", "claim"}
            and value["provider"] == "semantic-test-owner"
            and isinstance(value["claim"], str)
            and value["claim"],
            "invalid semantic-test owner identity",
        )
        return dict(value)


class ReferenceSession:
    def __init__(self, storage: ReferenceStorage, *, write: bool) -> None:
        self.storage = storage
        self.write = write
        self.active = True
        self.identity = storage.identity
        self.journal_id = storage.journal_id
        self.ledger = storage.ledger
        self.max_concurrency = storage.max_concurrency
        self.recovery_backup: Path | None = None

    def _writable(self, lease: InvocationLease | None, *, cancellation: bool = False) -> None:
        check(self.active and self.write, "reference session is closed or read-only")
        if not cancellation:
            check(lease is not None, "current invocation ownership is required")
            assert lease is not None
            check(
                lease.domain is self.storage.domain and self.storage.ownership.issued.get(lease.token) is lease,
                "ownership issuer differs from storage",
            )
            lease.assert_current()

    def _scope(self, lease: InvocationLease, identity: str, number: int, output: str | None = None) -> None:
        check(
            lease.reference.store == self.identity
            and lease.reference.execution_id == identity
            and lease.reference.number == number
            and (output is None or str(lease.reference.output) == output),
            "ownership scope differs from the attempted effect",
        )

    def get(self, identity: str) -> Doc | None:
        check(self.active, "reference session is closed")
        value = self.storage.records.get(identity)
        return copy.deepcopy(value) if value is not None else None

    def prepare(self, intent: Doc, *, lease: InvocationLease) -> Doc:
        self._writable(lease)
        check(intent["id"] == planning.digest(intent["identity"]), "work identity differs from its content")
        check(type(intent["max_attempts"]) is int and 1 <= intent["max_attempts"] <= 10, "invalid attempt budget")
        previous = self.storage.records.get(intent["id"])
        self._scope(lease, intent["id"], len(previous["attempts"]) + 1 if previous else 1)
        if previous is not None:
            check(previous["intent"] == intent, "work identity already has another immutable intent")
        else:
            self.storage.records[intent["id"]] = {
                "intent": copy.deepcopy(intent),
                "attempts": [],
                "phase": "prepared",
                "digest": planning.digest(intent),
            }
        value = self.get(intent["id"])
        assert value is not None
        return value

    def reserve(self, identity: str, data: Doc, *, lease: InvocationLease) -> Doc:
        self._writable(lease)
        value = self.storage.records[identity]
        attempts = value["attempts"]
        check(
            isinstance(data.get("authorization"), dict) and isinstance(data["authorization"].get("output"), str),
            "reservation has no output scope",
        )
        self._scope(lease, identity, len(attempts) + 1, data["authorization"]["output"])
        check(
            not attempts or attempts[-1]["phase"] in TERMINAL - {"succeeded"},
            "work has an unresolved or successful attempt",
        )
        check(len(attempts) < value["intent"]["max_attempts"], "attempt budget exhausted")
        occupied = sum(
            bool(item["attempts"]) and item["attempts"][-1]["phase"] not in TERMINAL
            for item in self.storage.records.values()
        )
        check(occupied < self.max_concurrency, "concurrency limit reached")
        event = self._event(1, "reserved", data, value["digest"])
        attempts.append(
            {"number": len(attempts) + 1, "events": [event], "phase": "reserved", "digest": event["digest"]}
        )
        value.update(phase="reserved", digest=event["digest"])
        result = self.get(identity)
        assert result is not None
        return result

    @staticmethod
    def _event(sequence: int, phase: str, data: Doc, previous: str) -> Doc:
        event = {
            "seq": sequence,
            "phase": phase,
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "data": copy.deepcopy(data),
            "previous": previous,
        }
        return {**event, "digest": planning.digest(event)}

    def append(
        self, identity: str, attempt: int, phase: str, data: Doc, *, lease: InvocationLease | None = None
    ) -> Doc:
        value = self.storage.records[identity]
        selected = value["attempts"][attempt - 1]
        cancellation = (
            data == {"kind": "cancellation_requested"}
            and phase == selected["phase"]
            and selected["phase"] not in TERMINAL
        )
        self._writable(lease, cancellation=cancellation and lease is None)
        if cancellation and any(event["data"] == data for event in selected["events"]):
            result = self.get(identity)
            assert result is not None
            return result
        if lease is not None:
            self._scope(lease, identity, attempt, selected["events"][0]["data"].get("authorization", {}).get("output"))
        if selected["phase"] in TERMINAL:
            check(
                phase == selected["phase"] and data == selected["events"][-1]["data"], "terminal receipt is immutable"
            )
        else:
            check(attempt == len(value["attempts"]), "only the current attempt may advance")
            allowed = {"running", *TERMINAL}
            if selected["phase"] == "reserved":
                allowed.add("reserved")
            check(phase in allowed, "invalid attempt phase transition")
            sequence = len(selected["events"]) + 1
            check(sequence <= (32 if phase in TERMINAL else 31), "event history limit reached")
            event = self._event(sequence, phase, data, selected["digest"])
            selected["events"].append(event)
            selected.update(phase=phase, digest=event["digest"])
            value.update(phase=phase, digest=event["digest"])
        result = self.get(identity)
        assert result is not None
        return result


class ReferenceStorage:
    """Serialized semantic reference; process-local memory deliberately supplies no durability."""

    def __init__(
        self, scope: Scope, *, journal_id: str, ownership: ReferenceOwnership, max_concurrency: int = 1
    ) -> None:
        self.scope = scope
        self.ownership = ownership
        self.domain = ownership.domain
        self.identity = StoreIdentity(str(scope.journal), journal_id)
        self.journal_id = journal_id
        self.ledger = str(scope.ledger)
        self.max_concurrency = max_concurrency
        self.records: dict[str, Doc] = {}
        self.lock = threading.Lock()
        self.opens: list[tuple[bool, bool]] = []

    @contextmanager
    def open(
        self, scope: Scope, *, write: bool, lock_timeout: float, coordinated: bool = False, recover: bool = False
    ) -> Iterator[AttemptSession]:
        check(scope.journal == self.scope.journal, "unknown reference journal")
        check(not recover, "memory reference has no interrupted SQLite recovery")
        if not self.lock.acquire(timeout=lock_timeout):
            raise CoordinatorLockTimeoutError("reference store lock timed out")
        session = ReferenceSession(self, write=write)
        self.opens.append((write, coordinated))
        try:
            yield session
        finally:
            session.active = False
            self.lock.release()


class ControlledWorker:
    """A deterministic protocol test double; it never launches candidate code."""

    def __init__(self, ownership: ReferenceOwnership) -> None:
        self.ownership = ownership
        self.domain = ownership.domain
        self.calls: list[str] = []
        self.running: set[str] = set()
        self.cleanup_available = True
        self.interrupt = False
        self.before_boundary: Callable[[], None] | None = None
        self.after_worker: Callable[[], None] | None = None
        self.mutate_result: Callable[[Doc], Doc] | None = None

    def validate_identity(self, value: Any) -> Doc:
        check(
            isinstance(value, dict)
            and set(value) == {"provider", "job"}
            and value["provider"] == "semantic-test-worker"
            and isinstance(value["job"], str)
            and bool(value["job"]),
            "invalid semantic-test worker handle",
        )
        return dict(value)

    def inspect(self, handle: WorkerHandle) -> bool:
        selected = self.validate_identity(handle.identity)
        self.calls.append("inspect")
        return selected["job"] in self.running

    def reconcile(self, identities: Sequence[WorkerHandle], *, lease: InvocationLease) -> list[Doc]:
        check(
            lease.domain is self.domain and self.ownership.issued.get(lease.token) is lease,
            "ownership issuer differs from worker",
        )
        lease.assert_current()
        check(
            all(handle.reference == lease.reference for handle in identities),
            "worker handle belongs to another invocation",
        )
        checked = [self.validate_identity(handle.identity) for handle in identities]
        self.calls.append("reconcile")
        check(self.cleanup_available, "termination remains unverified")
        result = []
        for selected in checked:
            self.running.discard(selected["job"])
            result.append({"identity": selected, "terminated": True, "reason": "terminated"})
        return result

    def supervise(
        self,
        spec: WorkerSpec,
        *,
        on_worker: Callable[[Doc], None],
        on_boundary: Callable[[Doc], None],
        is_cancelled: Callable[[], bool],
        lease: InvocationLease,
    ) -> Doc:
        check(
            lease.domain is self.domain and self.ownership.issued.get(lease.token) is lease,
            "ownership issuer differs from worker",
        )
        lease.assert_current()
        check(
            spec.reference == lease.reference and spec.out == lease.reference.output,
            "worker output or reference differs from owned attempt",
        )
        self.calls.append("supervise")
        worker = {"provider": "semantic-test-worker", "job": uuid.uuid4().hex}
        boundary = {"provider": "semantic-test-worker", "job": uuid.uuid4().hex}
        self.running.add(worker["job"])
        on_worker(worker)
        if self.after_worker is not None:
            self.after_worker()
        lease.assert_current()
        if self.before_boundary is not None:
            self.before_boundary()
        self.running.add(boundary["job"])
        on_boundary(boundary)
        lease.assert_current()
        if self.interrupt:
            raise ValueError("controlled supervisor interruption")
        cancelled = is_cancelled()
        proofs = self.reconcile(
            [WorkerHandle(spec.reference, worker), WorkerHandle(spec.reference, boundary)], lease=lease
        )
        if not cancelled:
            self._publish(spec)
        result: Doc = {
            "status": "cancelled" if cancelled else "completed",
            "reason": None,
            "report": None,
            "termination": {"worker": proofs[0], "namespace": proofs[1]},
        }
        return self.mutate_result(result) if self.mutate_result is not None else result

    @staticmethod
    def _publish(spec: WorkerSpec) -> None:
        spec.out.mkdir(mode=0o700)
        (spec.out / "artifacts").mkdir(mode=0o700)
        payload = b"controlled protocol-test artifact\n"
        (spec.out / "artifacts/result.txt").write_bytes(payload)
        (spec.out / "stdout.bin").write_bytes(b"")
        (spec.out / "stderr.bin").write_bytes(b"")
        from exacly.executor_contract import roster

        report = {
            "schema_version": isolation.VERSION,
            "backend": isolation.BACKEND,
            "status": "completed",
            "exit_code": 0,
            "argv": list(spec.argv),
            "runtime": {key: spec.runtime[key] for key in ("rootfs_digest", "python")},
            "isolation": {"namespace_terminated": True},
            "inputs": roster(spec.inputs),
            "stdout": {"path": "stdout.bin", "size": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest()},
            "stderr": {"path": "stderr.bin", "size": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest()},
            "artifacts": roster({"artifacts/result.txt": payload}),
            "claims": {
                "outputs_trusted": False,
                "evidence_admitted": False,
                "completion_authorized": False,
                "self_authenticating": False,
            },
        }
        report["digest"] = planning.digest(report)
        (spec.out / "report.json").write_text(json.dumps(report))


def reference_bundle(
    scope: Scope, journal_id: str, *, max_concurrency: int = 1
) -> tuple[CoordinatorBackends, ReferenceStorage, ReferenceOwnership, ControlledWorker]:
    ownership = ReferenceOwnership()
    storage = ReferenceStorage(scope, journal_id=journal_id, ownership=ownership, max_concurrency=max_concurrency)
    worker = ControlledWorker(ownership)
    return CoordinatorBackends(storage, ownership, worker), storage, ownership, worker

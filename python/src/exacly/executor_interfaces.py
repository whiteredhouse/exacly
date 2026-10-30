"""Trusted coordinator ports for attempt storage, invocation ownership and workers.

These protocols describe semantic obligations, not evidence that an implementation satisfies them.
Select implementations through trusted Python construction. Candidate policies cannot load providers.
Version 1 supports a single local-machine coordination domain. Distributed ownership remains
unsupported: a future contract must enforce fencing tokens atomically at storage and worker effects.
An ownership check followed by an unrelated write is not distributed fencing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

Doc = dict[str, Any]
VERSION = "0.1.0"
LOCAL_SCOPE = "local-machine"
LOCAL_ADAPTER = "local-bubblewrap-v1"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted", "output_invalid", "timed_out", "output_limit"})
IdentityValidator = Callable[[Any], Doc]


class CoordinatorLockTimeoutError(ValueError):
    """The backend's bounded coordination wait expired without authorizing another effect."""


@dataclass(frozen=True)
class Scope:
    """Resources selected by the existing local policy and authority facade.

    Coordinated sessions protect the attempt journal and the selected binding and state resources
    for the complete decision/reservation operation. Provider implementations own their locks and
    transactions; a session never exposes a database connection or descriptor to the coordinator.
    """

    journal: Path
    ledger: Path | None = None
    state: Path | None = None


@dataclass(frozen=True)
class StoreIdentity:
    """Canonical backend locator and initialized store identity, independent of a connection."""

    locator: str
    instance_id: str


@dataclass(frozen=True)
class AttemptReference:
    """Exact work, ordinal and output destination to which an invocation claim belongs."""

    execution_id: str
    number: int
    output: Path
    store: StoreIdentity


class InvocationLease(Protocol):
    """One held invocation claim, passed to every owned storage and worker effect.

    Tokens are opaque to the coordinator. Implementations must reject a released, stale or
    differently scoped claim at their effect boundary. Local non-expiring locks do not establish
    ownership across machines. An assertion is an additional check, never proof of remote fencing.
    """

    @property
    def token(self) -> object: ...

    @property
    def domain(self) -> object: ...

    @property
    def reference(self) -> AttemptReference: ...

    @property
    def owner_identity(self) -> Doc: ...

    def assert_current(self) -> None: ...


class AttemptSession(Protocol):
    """A serialized checked view with durable, immutable transitions.

    Reservation must atomically consume the next bounded attempt and a shared capacity slot.
    Unresolved attempts keep that slot until a terminal receipt retains verified termination.
    A failed or interrupted call never authorizes a fresh reservation implicitly. Implementations
    reject changed contracts, stale claims and terminal rewrites. The sole unowned mutation is an
    exact cancellation-request event for the current unresolved attempt; it grants no launch right.
    """

    @property
    def identity(self) -> StoreIdentity: ...

    @property
    def journal_id(self) -> str: ...

    @property
    def ledger(self) -> str: ...

    @property
    def max_concurrency(self) -> int: ...

    @property
    def recovery_backup(self) -> Path | None: ...

    def get(self, identity: str) -> Doc | None: ...

    def prepare(self, intent: Doc, *, lease: InvocationLease) -> Doc: ...

    def reserve(self, identity: str, data: Doc, *, lease: InvocationLease) -> Doc: ...

    def append(
        self, identity: str, attempt: int, phase: str, data: Doc, *, lease: InvocationLease | None = None
    ) -> Doc: ...


class AttemptStorage(Protocol):
    """Open existing attempt history without implicit initialization or repair.

    Coordinated sessions serialize journal, binding and state decisions as one protected operation.
    Lock waits share the supplied finite budget. Explicit recovery preserves protected exact input
    backups before repair and never starts workers, changes authoritative state or resets budgets.
    """

    @property
    def domain(self) -> object: ...

    def open(
        self,
        scope: Scope,
        *,
        write: bool,
        lock_timeout: float,
        coordinated: bool = False,
        recover: bool = False,
    ) -> AbstractContextManager[AttemptSession]: ...


class InvocationOwnership(Protocol):
    """Claim a current invocation without confusing a live process with active supervision."""

    @property
    def domain(self) -> object: ...

    def claim(self, reference: AttemptReference, *, create: bool) -> AbstractContextManager[InvocationLease | None]: ...

    def validate_identity(self, value: Any) -> Doc: ...


@dataclass(frozen=True)
class WorkerHandle:
    """An opaque worker identity targeted to one checked attempt.

    This envelope is not authentication. The trusted coordinator constructs it only from the
    selected validated history while holding its invocation claim. A worker adapter checks the
    complete target before inspecting or signaling, then proves the underlying exact identity.
    """

    reference: AttemptReference
    identity: Doc


@dataclass(frozen=True)
class WorkerSpec:
    """Exactly approved snapshots and execution limits, with no operator storage handles."""

    runtime: Doc
    argv: tuple[str, ...]
    inputs: Mapping[str, bytes]
    out: Path
    timeout: float
    reference: AttemptReference


class WorkerLifecycle(Protocol):
    """A trusted lifecycle adapter with gated launch and exact termination evidence.

    Supervision includes launch, status, cancellation and the full worker deadline. Before starting
    a worker and before releasing candidate execution, the corresponding callback must durably
    retain its opaque identity. The coordinator detaches callback data and validates its identity
    and event ordering before append. A rejected callback starts no candidate. Cancellation and
    recovery do not clear capacity until reconcile proves termination of the exact retained workers.
    Implementations enforce the selected isolation contract independently of their own reports.
    """

    @property
    def domain(self) -> object: ...

    def supervise(
        self,
        spec: WorkerSpec,
        *,
        on_worker: Callable[[Doc], None],
        on_boundary: Callable[[Doc], None],
        is_cancelled: Callable[[], bool],
        lease: InvocationLease,
    ) -> Doc: ...

    def inspect(self, handle: WorkerHandle) -> bool: ...

    def reconcile(self, handles: Sequence[WorkerHandle], *, lease: InvocationLease) -> list[Doc]: ...

    def validate_identity(self, value: Any) -> Doc: ...


@dataclass(frozen=True)
class CoordinatorBackends:
    """Explicit trusted implementations used by the coordinator's complete lifecycle.

    Structural typing and these declarations do not establish storage durability, isolation or
    fencing. Conformance and physical runtime tests must verify each implementation independently.
    """

    storage: AttemptStorage
    ownership: InvocationOwnership
    workers: WorkerLifecycle
    scope: str = LOCAL_SCOPE
    protocol_version: str = VERSION
    contract_adapter: str = LOCAL_ADAPTER

    def require_scope(self, requested: str = LOCAL_SCOPE) -> None:
        if (
            self.protocol_version != VERSION
            or self.scope != LOCAL_SCOPE
            or requested != LOCAL_SCOPE
            or self.contract_adapter != LOCAL_ADAPTER
        ):
            raise ValueError(
                "coordinator protocol 0.1.0 supports only local-machine ownership; "
                "distributed coordination and fencing require a separately verified backend contract"
            )
        if not (self.storage.domain is self.ownership.domain is self.workers.domain):
            raise ValueError("coordinator backends must share one trusted ownership domain")

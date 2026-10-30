"""Reviewed local implementations of the coordinator's storage, ownership and worker ports.

SQLite connections, directory descriptors, process identities and Bubblewrap remain implementation
details here. These adapters provide exclusive lifetime ownership on one host. They do not provide
distributed leases or make a check-then-act ownership assertion equivalent to distributed fencing.
"""

from __future__ import annotations

import copy
import os
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from exacly import execution_store, executor_runtime, executor_store, isolation, operation_locks
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

try:
    import fcntl
except ImportError:  # pragma: no cover - this implementation requires POSIX ownership locks.
    fcntl = None  # type: ignore[assignment]

Doc = dict[str, Any]


class _LocalDomain:
    """Construction capability shared by the reviewed local adapters, never a fencing claim."""

    def __init__(self) -> None:
        self._claims: dict[object, _LocalLease] = {}

    def check(self, lease: InvocationLease) -> _LocalLease:
        try:
            retained = self._claims.get(lease.token)
        except (AttributeError, TypeError):
            raise ValueError("invocation ownership was not issued by these local components") from None
        execution_store.require(
            retained is lease and lease.domain is self,
            "invocation ownership was not issued by these local components",
        )
        assert retained is not None
        retained.assert_current()
        return retained


def _domain(value: object | None) -> _LocalDomain:
    if value is None:
        return _LocalDomain()
    execution_store.require(isinstance(value, _LocalDomain), "invalid local coordinator construction domain")
    assert isinstance(value, _LocalDomain)
    return value


class _LocalSession:
    """A narrow view whose lifetime is the adapter's checked, locked store context."""

    def __init__(self, journal: executor_store.Journal, domain: _LocalDomain, identity: StoreIdentity) -> None:
        self._journal = journal
        self._domain = domain
        self._identity = identity
        self._active = True

    def _require_active(self) -> None:
        execution_store.require(self._active, "coordinator attempt session is closed")

    def _require_write(self) -> None:
        self._require_active()
        execution_store.require(self._journal.write, "coordinator attempt session is read-only")

    @property
    def identity(self) -> StoreIdentity:
        self._require_active()
        return self._identity

    @property
    def journal_id(self) -> str:
        self._require_active()
        return self._journal.journal_id

    @property
    def ledger(self) -> str:
        self._require_active()
        return self._journal.ledger

    @property
    def max_concurrency(self) -> int:
        self._require_active()
        return self._journal.max_concurrency

    @property
    def recovery_backup(self) -> Path | None:
        self._require_active()
        return self._journal.recovery_backup

    def get(self, identity: str) -> Doc | None:
        self._require_active()
        return self._journal.get(identity)

    def prepare(self, intent: Doc, *, lease: InvocationLease) -> Doc:
        self._require_write()
        retained = self._domain.check(lease)
        current = self._journal.get(intent["id"])
        self._check_reference(retained.reference, intent["id"], len(current["attempts"]) + 1 if current else 1)
        return self._journal.prepare(intent)

    def reserve(self, identity: str, data: Doc, *, lease: InvocationLease) -> Doc:
        self._require_write()
        retained = self._domain.check(lease)
        current = self._journal.get(identity)
        execution_store.require(current is not None, "executor intent does not exist")
        assert current is not None
        self._check_reference(retained.reference, identity, len(current["attempts"]) + 1)
        execution_store.require(
            isinstance(data.get("authorization"), dict)
            and data["authorization"].get("output") == str(retained.reference.output),
            "reservation output differs from invocation ownership",
        )
        return self._journal.reserve(identity, data)

    def _check_reference(self, reference: AttemptReference, identity: str, attempt: int) -> None:
        execution_store.require(
            reference.store == self._identity and reference.execution_id == identity and reference.number == attempt,
            "invocation ownership belongs to another store, work item or attempt",
        )

    def append(
        self, identity: str, attempt: int, phase: str, data: Doc, *, lease: InvocationLease | None = None
    ) -> Doc:
        self._require_write()
        if lease is None:
            current = self._journal.get(identity)
            execution_store.require(
                data == {"kind": "cancellation_requested"}
                and current is not None
                and bool(current["attempts"])
                and current["attempts"][-1]["number"] == attempt
                and current["attempts"][-1]["phase"] not in executor_store.TERMINAL,
                "only cancellation of the current unresolved attempt may omit ownership",
            )
            assert current is not None
            if any(event["data"] == {"kind": "cancellation_requested"} for event in current["attempts"][-1]["events"]):
                return current
            execution_store.require(
                current["attempts"][-1]["phase"] == phase,
                "cancellation phase differs from the current unresolved attempt",
            )
        else:
            retained = self._domain.check(lease)
            self._check_reference(retained.reference, identity, attempt)
            current = self._journal.get(identity)
            execution_store.require(
                current is not None and 1 <= attempt <= len(current["attempts"]), "executor attempt does not exist"
            )
            assert current is not None
            authorization = current["attempts"][attempt - 1]["events"][0]["data"].get("authorization")
            execution_store.require(
                isinstance(authorization, dict) and authorization.get("output") == str(retained.reference.output),
                "retained attempt output differs from invocation ownership",
            )
        return self._journal.append(identity, attempt, phase, data)


class LocalAttemptStorage:
    """Open the existing private format and own its ordered local coordination boundary."""

    def __init__(self, domain: object | None = None) -> None:
        self._domain = _domain(domain)

    @property
    def domain(self) -> object:
        return self._domain

    @contextmanager
    def open(
        self,
        scope: Scope,
        *,
        write: bool,
        lock_timeout: float,
        coordinated: bool = False,
        recover: bool = False,
    ) -> Iterator[AttemptSession]:
        budget = execution_store.Deadline(lock_timeout)
        try:
            if coordinated:
                execution_store.require(
                    scope.ledger is not None and scope.state is not None,
                    "coordinated attempt storage requires binding and state authority",
                )
                assert scope.ledger is not None and scope.state is not None
                with (
                    operation_locks.directories(
                        (scope.journal.parent, scope.ledger.parent, scope.state.parent), write=write, deadline=budget
                    ),
                    executor_store.opened(
                        scope.journal, write=write, deadline=budget, already_locked=True, recover=recover
                    ) as journal,
                    self._session(journal, scope) as session,
                ):
                    yield session
            else:
                with (
                    executor_store.opened(scope.journal, write=write, deadline=budget, recover=recover) as journal,
                    self._session(journal, scope) as session,
                ):
                    yield session
        except execution_store.LockTimeoutError as error:
            raise CoordinatorLockTimeoutError(str(error)) from None

    @contextmanager
    def _session(self, journal: executor_store.Journal, scope: Scope) -> Iterator[AttemptSession]:
        if scope.ledger is not None:
            execution_store.require(
                journal.ledger == str(scope.ledger.resolve()),
                "attempt storage belongs to another binding ledger",
            )
        session = _LocalSession(
            journal, self._domain, StoreIdentity(str(execution_store.location(scope.journal)), journal.journal_id)
        )
        try:
            yield session
        finally:
            session._active = False


class _LocalLease:
    """Opaque local claim; only this adapter can see its descriptor and inode anchor."""

    def __init__(self, path: Path, descriptor: int, domain: _LocalDomain, reference: AttemptReference) -> None:
        self._path = path
        self._descriptor = descriptor
        info = execution_store.file_info(descriptor)
        self._inode = info.st_dev, info.st_ino
        parent = path.parent.lstat()
        self._parent_inode = parent.st_dev, parent.st_ino
        self._owner = executor_runtime.process_identity()
        self._token = object()
        self._domain = domain
        self._reference = reference
        self._active = True
        self._domain._claims[self._token] = self

    @property
    def domain(self) -> object:
        return self._domain

    @property
    def reference(self) -> AttemptReference:
        return self._reference

    @property
    def token(self) -> object:
        return self._token

    @property
    def owner_identity(self) -> Doc:
        self.assert_current()
        return copy.deepcopy(self._owner)

    def assert_current(self) -> None:
        execution_store.require(self._active, "coordinator invocation ownership is no longer current")
        execution_store.require(
            self._domain._claims.get(self._token) is self,
            "coordinator invocation ownership is no longer issued",
        )
        execution_store.require(
            self._path.parent == self._path.parent.resolve(),
            "executor ownership directory traverses a symbolic link",
        )
        parent = self._path.parent.lstat()
        execution_store.require(
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == os.getuid()
            and stat.S_IMODE(parent.st_mode) == 0o700
            and (parent.st_dev, parent.st_ino) == self._parent_inode,
            "executor ownership directory changed",
        )
        opened = execution_store.file_info(self._descriptor)
        current = self._path.lstat()
        execution_store.require(
            (opened.st_dev, opened.st_ino) == self._inode == (current.st_dev, current.st_ino),
            "executor invocation ownership file changed",
        )


class LocalInvocationOwnership:
    """Exclusive lifetime ownership; no timeout or clock grants replacement ownership."""

    def __init__(self, domain: object | None = None) -> None:
        self._domain = _domain(domain)

    @property
    def domain(self) -> object:
        return self._domain

    def validate_identity(self, value: Any) -> Doc:
        return executor_runtime.validate_identity(value)

    @contextmanager
    def claim(self, reference: AttemptReference, *, create: bool) -> Iterator[InvocationLease | None]:
        execution_store.require(fcntl is not None, "executor ownership leases require POSIX locks")
        execution_store.require(
            isinstance(reference, AttemptReference)
            and isinstance(reference.execution_id, str)
            and execution_store.DIGEST.fullmatch(reference.execution_id)
            and type(reference.number) is int
            and 1 <= reference.number <= executor_store.MAX_ATTEMPTS
            and isinstance(reference.output, Path),
            "invalid coordinator attempt reference",
        )
        execution_store.require(
            isinstance(reference.store, StoreIdentity)
            and isinstance(reference.store.locator, str)
            and isinstance(reference.store.instance_id, str)
            and bool(reference.store.locator)
            and bool(reference.store.instance_id),
            "invalid coordinator attempt store identity",
        )
        output = reference.output
        if create:
            output.parent.mkdir(mode=0o700, exist_ok=True)
            execution_store.require(
                not os.path.lexists(output), "executor attempt output already exists; inspect retained work"
            )
        path = output.with_name("." + output.name + ".owner")
        execution_store.require(
            path.parent == path.parent.resolve(), "executor ownership directory traverses a symbolic link"
        )
        parent_info = path.parent.lstat()
        execution_store.require(
            stat.S_ISDIR(parent_info.st_mode)
            and parent_info.st_uid == os.getuid()
            and stat.S_IMODE(parent_info.st_mode) == 0o700,
            "executor ownership directory must remain owner-private",
        )
        descriptor = os.open(
            path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_CREAT if create else 0), 0o600
        )
        lease: _LocalLease | None = None
        try:
            before = execution_store.file_info(descriptor)
            acquired = False
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            current = path.lstat()
            execution_store.require(
                (before.st_dev, before.st_ino) == (current.st_dev, current.st_ino),
                "executor ownership lease changed",
            )
            if create:
                os.fsync(descriptor)
                for folder in (path.parent, path.parent.parent):
                    parent = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(parent)
                    finally:
                        os.close(parent)
            if acquired:
                lease = _LocalLease(path, descriptor, self._domain, reference)
                lease.assert_current()
            yield lease
        finally:
            if lease is not None:
                lease._active = False
                self._domain._claims.pop(lease.token, None)
            os.close(descriptor)


class LocalWorkerLifecycle:
    """Adapt trusted local runtime operations without exposing process plumbing to the core."""

    def __init__(self, domain: object | None = None) -> None:
        self._domain = _domain(domain)

    @property
    def domain(self) -> object:
        return self._domain

    def validate_identity(self, value: Any) -> Doc:
        return executor_runtime.validate_identity(value)

    def inspect(self, handle: WorkerHandle) -> bool:
        execution_store.require(isinstance(handle, WorkerHandle), "a scoped worker handle is required")
        return executor_runtime.alive(handle.identity)

    def reconcile(self, handles: Sequence[WorkerHandle], *, lease: InvocationLease) -> list[Doc]:
        retained = self._domain.check(lease)
        execution_store.require(
            all(isinstance(handle, WorkerHandle) and handle.reference == retained.reference for handle in handles),
            "worker recovery belongs to another invocation reference",
        )
        identities = [executor_runtime.validate_identity(handle.identity) for handle in handles]
        return executor_runtime.terminate(identities)

    def supervise(
        self,
        spec: WorkerSpec,
        *,
        on_worker: Callable[[Doc], None],
        on_boundary: Callable[[Doc], None],
        is_cancelled: Callable[[], bool],
        lease: InvocationLease,
    ) -> Doc:
        retained = self._domain.check(lease)
        execution_store.require(
            retained.reference == spec.reference and retained.reference.output == spec.out,
            "worker reference or output differs from invocation ownership",
        )

        def retain(callback: Callable[[Doc], None], identity: Doc) -> None:
            self._domain.check(lease)
            callback(identity)
            self._domain.check(lease)

        def cancelled() -> bool:
            self._domain.check(lease)
            return is_cancelled()

        return executor_runtime.run(
            isolation.parse_policy(spec.runtime),
            spec.argv,
            spec.inputs,
            spec.out,
            timeout=spec.timeout,
            on_worker=lambda identity: retain(on_worker, identity),
            on_namespace=lambda identity: retain(on_boundary, identity),
            is_cancelled=cancelled,
        )


def local_backends() -> CoordinatorBackends:
    """Construct the reviewed local components; candidate configuration never loads providers."""
    domain = _LocalDomain()
    return CoordinatorBackends(
        LocalAttemptStorage(domain), LocalInvocationOwnership(domain), LocalWorkerLifecycle(domain)
    )

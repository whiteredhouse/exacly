# Coordinator backend contracts

The coordinator uses explicit interfaces for attempt storage, invocation ownership and worker lifecycle. [REQ-031](../spec/exacly/automation.yaml) defines their obligations. `exacly.executor_interfaces` supplies the Python protocols and composition types; `exacly.executor_local` supplies the supported local implementations. This is unreleased functionality on main.

These interfaces provide tested extension points for another coordinator implementation. Protocol version `0.1.0` supports one `local-machine` coordination domain. The production backend remains the private SQLite companion, exclusive local ownership and verified Linux Bubblewrap worker described in the [bounded executor guide](bounded-executor.md). Remote providers and distributed ownership require their own reviewed contract and real verification before support can be claimed.

| Contract | Responsibility | Local implementation |
| --- | --- | --- |
| `AttemptStorage` and `AttemptSession` | Checked history, atomic reservations and bounded capacity | `LocalAttemptStorage` |
| `InvocationOwnership` and `InvocationLease` | Current ownership of one exact attempt | `LocalInvocationOwnership` |
| `WorkerLifecycle` | Gated supervision, inspection and verified reconciliation | `LocalWorkerLifecycle` |

## Compose trusted implementations

`CoordinatorBackends` contains `storage`, `ownership` and `workers`, plus its protocol version, scope and `contract_adapter`. All three implementations must share the same trusted ownership domain, and effectful implementations must recognize the actual issuer and current claim rather than trust copied domain or token declarations. Version `0.1.0` accepts the existing `local-bubblewrap-v1` contract adapter and `local-machine` scope. An incompatible version, unsupported scope or adapter, or mismatched domain is rejected before execution effects.

The optional `backends=` Python argument selects this bundle for `preview`, `apply`, `show`, `cancel`, `recover` and `recover_journal`. Omitting it selects `local_backends()`. These functions also accept `coordination_scope=`, whose supported value is `local-machine`. The CLI continues to select the local implementation; policy files cannot name Python modules, load plugins or choose an alternate provider. Backend selection is trusted operator application code with the authority to construct the coordinator.

```python
from exacly import executor
from exacly.executor_local import local_backends

backends = local_backends()
proposal = executor.preview(documents, state_path, request, backends=backends)

# Independently review the exact proposal before supplying its authorization.
result = executor.apply(
    journal,
    ledger,
    documents,
    state_path,
    request,
    authorization=reviewed_authorization,
    backends=backends,
)
```

Use the same reviewed composition and canonical journal throughout inspection and recovery. Backend compatibility, a protocol declaration or an `isolation_verified` field does not establish trust. Each actual implementation must demonstrate its required storage, ownership and execution guarantees. Candidate processes receive approved snapshots through the worker interface and never receive the backend objects, ownership tokens or operator storage handles.

## Storage and protected sessions

`Scope` identifies the selected journal and, for coordinated decisions, the binding ledger and actual state resource. `AttemptStorage.open()` owns the backend's locking and transaction machinery. With `coordinated=True`, it protects the journal, binding and state checks through the decision and reservation boundary. `lock_timeout` bounds coordination waits. Ordinary opening never initializes, migrates or repairs storage; explicit recovery retains protected exact backups before repair.

The returned `AttemptSession` exposes its checked `identity: StoreIdentity`, initialized journal identity, selected binding ledger, fixed concurrency limit and any recovery backup, followed by `get`, `prepare`, `reserve` and `append`. `StoreIdentity(locator, instance_id)` binds a canonical backend destination to its initialized instance. For the local adapter these are the canonical journal path and its UUID. It does not expose a SQLite connection or a file descriptor. A session is valid only inside its context.

Storage implementations must preserve these semantics:

- A work identity has one immutable approved contract and original attempt budget. A changed policy or destination cannot reset it.
- Reservation consumes the next attempt ordinal and a shared concurrency slot atomically before a worker can start. An unresolved attempt keeps its slot.
- A protected operation observes one checked history; a stale authorization, changed head or conflicting reservation does not start another effect.
- Terminal receipts and successful effects are immutable. Exact replay returns the retained result, while a different request cannot replace it.
- Capacity is released only by a terminal outcome with the required verified process or provider termination facts. A clock, heartbeat or apparent worker inactivity alone does not prove termination.
- The coordinator validates a complete proposed terminal outcome against the selected checked history before appending it. Invalid worker results or insufficient termination facts leave the attempt unresolved and its capacity occupied.

`LocalAttemptStorage` adapts the existing version-1 executor journal and ordered local directory locks. The version-2 and version-4 binding/completion ledgers and the existing journal application ID, schema, identities and authorization formats remain unchanged. Generic orchestration does not bypass the existing protected-path, SQLite sidecar or retained-history checks.

## Invocation ownership

`AttemptReference` identifies one `execution_id`, attempt `number`, `output` destination and checked `store: StoreIdentity`. `InvocationOwnership.claim(reference, create=...)` yields an `InvocationLease` or reports that the invocation is already owned. A lease supplies an opaque `token`, its ownership `domain`, that exact `reference`, a copied owner identity and `assert_current()`. The coordinator passes the lease to owned storage mutations and worker effects. The implementation must reject a released, stale or differently scoped claim where that effect occurs, including a claim issued for another initialized store, work identity or ordinal.

The sole unowned attempt mutation is an exact cancellation request for the current unresolved attempt. It asks the active owner to stop and provides no permission to launch work, finalize a receipt or free capacity.

`LocalInvocationOwnership` holds an exclusive lifetime lock on the private ownership file beside the attempt output. It verifies the held file and directory identities and distinguishes an active invocation from an idle long-lived API process. Releasing an invocation's lock permits recovery to reconcile its retained workers; it does not itself prove they have stopped. Local tokens are process-local opaque objects and are not remote lease or fencing credentials.

## Worker lifecycle and identities

`WorkerSpec` contains the reviewed runtime description, argument vector, immutable input snapshots, output destination, finite timeout and exact `reference: AttemptReference`. A worker effect must match that reference to the current lease. `WorkerLifecycle.supervise()` covers launch, status, cancellation and the worker deadline. It receives the current lease and three callbacks:

- `on_worker` durably retains the provider-owned worker identity before its startup gate opens.
- `on_boundary` durably retains the actual execution-boundary identity before candidate code is released.
- `is_cancelled` reports a retained cancellation request without allowing a storage wait to extend the candidate deadline.

A rejected callback prevents candidate release. `WorkerHandle(reference, identity)` wraps a provider-owned identity with the exact attempt reference for live inspection and reconciliation. The coordinator constructs these handles from the selected validated history; the wrapper is not authentication and does not replace validation of the provider's actual identity. `inspect()` checks the exact retained identity's liveness without granting completion or capacity-release authority. `reconcile()` must reject handles scoped to another invocation and establish termination of the retained workers before returning termination facts that can support a terminal outcome. A partial or uncertain result leaves the attempt accounted for.

Supervision returns a result document with `status` and `termination`, plus optional diagnostic `reason` text and a collected `report`. The supported status strings are `completed`, `candidate_failed`, `runtime_failed`, `interrupted`, `timed_out`, `output_limit` and `cancelled`. An unknown or non-string status is rejected before a terminal receipt can be appended. `termination` must cover the exact retained worker and boundary identities with validated termination facts; malformed or incomplete facts leave the attempt unresolved.

`completed` still requires independent validation of the fixed private output bundle and required artifact identities. The result's `report` field alone supplies no successful-build authority. An explicit `candidate_failed` outcome remains failed, and known timeout, output-limit or cancellation outcomes remain unsuccessful even if a complete bundle is present. An uncertain lifecycle failure or interruption can reconcile a bundle that was already published before the result was lost. A retained cancellation request continues to take precedence. The complete prospective receipt is checked against the selected history before it is committed.

Worker and owner identity validation belong to their selected implementations. The coordinator treats provider identities as opaque documents instead of assuming that every worker has a Linux PID. `WorkerHandle` and `AttemptReference` are live interface values; existing version-1 history retains its original raw identity documents. `LocalWorkerLifecycle` delegates to the existing gated worker and Bubblewrap launcher; it retains the boot, process start-time and namespace checks and verifies termination through pinned kernel process handles. Its identity codec continues to validate existing local history strictly. This separation does not permit an alternate backend to reinterpret an existing local journal as another provider's history.

The local worker's independent kernel deadline covers staging, isolated execution and collection even while the supervisor is paused. Local process identifiers and `pidfd` operations remain inside the local adapters. The current worker policy remains closed to the supported local isolation backend, and candidate output remains untrusted throughout supervision and recovery.

## Conformance and physical verification

The common conformance suite exercises the local adapters and an independent reference implementation. It checks immutable contracts, reservation and concurrency accounting, retained budgets, exact replay, cancellation, session lifetime, stale ownership and recovery ordering. Coordinator substitution tests use provider-style opaque identities to demonstrate that orchestration calls the interfaces rather than falling back to local process machinery.

The reference implementation is controlled test infrastructure. An in-memory store demonstrates protocol semantics and does not establish crash durability; a controlled worker establishes interaction and authority rules and does not establish a physical isolation boundary. The existing real SQLite process-kill tests, Linux runtime tests, executor fault tests and installed-package checks remain necessary evidence for the supported local implementation. Any future backend needs equivalent checks against its actual storage and runtime.

The optional `mise run check-core` subset keeps the controlled `test_coordinator_backends.py` substitution cases. It omits `test_executor_interfaces.py` and `test_executor_local.py`, whose local ownership cases require Linux process identity and inspection interfaces, together with the other local execution test modules. This subset establishes neither the full REQ-031 contract nor REQ-025/027 verification; only the complete required check refreshes state and enters the verification gate.

## Requirements for another coordinator

Before approving another backend, specify its authority scope, stable identities, durable reservation and replay behavior, budget and concurrency boundary, worker-control semantics, output identity and recoverable failure states. Preserve independent evidence admission and completion authority.

A distributed backend additionally needs ownership epochs or fencing tokens checked atomically at each storage transition and worker-command boundary. A worker must reject a stale owner's command even if that owner passed an earlier ownership check. Heartbeats, time-based lease expiry and a check followed by an unrelated write do not provide that guarantee. Test takeover, stale owners, ambiguous worker requests, partial termination and recovery against the actual service. Protocol `0.1.0` rejects distributed scope rather than treating local ownership as evidence for these guarantees.

Migration must preserve the authoritative work and attempt identities, consumed budgets, unresolved reservations and immutable receipts. Reconcile or terminate existing attempts before a handoff, review the destination and its trust boundary, and retain explicit migration evidence. Starting a new empty backend is not a supported way to reset exhausted limits or erase uncertain work. A remote adapter will also need a separately reviewed provider and policy contract; the current local policy and paths do not silently become remote execution authority.

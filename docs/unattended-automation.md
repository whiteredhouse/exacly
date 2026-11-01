# Unattended verification foundations

The remaining unattended verification contracts, REQ-034–035 and REQ-037–039, are proposed in
[`spec/exacly/unattended-automation.yaml`](../spec/exacly/unattended-automation.yaml).
The implementation checkpoint is [`automation-progress.md`](automation-progress.md).
REQ-036 describes the implemented fixed inventory worker and resource integration. These components
do not enable a complete unattended workflow by themselves.

## Objective accounting

`exacly.standing_authorization` supplies protected local accounting for a trusted authorizer.
It records decisions made by that authorizer; it does not independently approve a delegation,
evaluate native effect preconditions, or turn caller-supplied authorization content into permission.
Candidate code must have no access to the API or its storage. Native dispatch, executor, handoff,
registration and completion authorization remain necessary.

Initialize its separate store in an owner-private directory outside Git and pin the returned store
identity in trusted operator configuration. Enroll the original objective and its fixed finite
limits once. Attempts, reserved wall time, model requests and model spend share that objective
across plans, actions, output revisions, policy versions and workflow aliases. Pinning a fresh store
is not a supported migration or budget reset; replacing that trusted configuration requires
independent approval and preserved prior accounting.

Before releasing any authority, record the exact proposal, preconditions, native authorization,
trusted decision and cost atomically with the reservation. A replay identifies a retained decision;
it does not authorize a second request. Uncertain and terminal effects retain their reservation.
Outcomes cannot refund capacity or change the enrolled limits. Native journals establish whether
an effect happened, and the controller must reconcile them before any retry.

The separate `automation_authorization` evaluator selects independently approved scope, preserves
exact native grants and meters reads, recovery and cancellation against this store. Protected
revocation facts live in `automation_revocations`. See [standing-authorization.md](standing-authorization.md)
for bootstrap ordering, native configuration pins and goal-wide exclusion. Accounting tests alone
establish accounting semantics; whole-workflow and live contracts require their own coverage.

The Python API exposes `initialize`, `enroll`, `reserve`, `inspect`, `mark_uncertain` and `settle`.
`reserve(..., approved=True, reason=...)` records a trusted caller's decision; the boolean is not
an approval mechanism and this API must never be exposed to candidate callers. Costs and limits
use exactly `attempts`, `wall_time_ms`, `model_requests` and `model_spend_microunits`, each a finite
nonnegative integer. Exact request replay returns its current retained status without recharging.

There is no supported SQLite hot-journal recovery or store migration API in this foundation.
If an interrupted transaction leaves a sidecar, preserve the database and sidecar together and
escalate. Normal APIs refuse them before SQLite can recover implicitly. Never delete the sidecar
or initialize a replacement store to resume with fresh capacity.

## Immutable worker inputs

`exacly.worker_integration.prepare` reads explicit regular source blobs from an initialized sealed
bare Git repository at a full approved commit. The trusted caller supplies its repository UUID,
literal allowed files, consumer-specific protected paths and separate approved model digest.
Built-in model, verifier, policy and reserved worker paths remain excluded. Preparation does not
check out source or run hooks, filters or candidate code.

The immutable result supplies `inputs`, `identity`, fixed `argv` and `expected_outputs` for a
separately authorized native executor request. Recipe `snapshot-inventory-v1` runs
`/usr/bin/python3 -I -S /inputs/.exacly-worker.py` and inventories source-byte digests into
`/outputs/source-digests.json`. It does not execute or verify those source files. Its retained
identity includes the exact commit, tree, blobs, snapshot, recipe and input bytes.

The concrete integration also supplies `bind`, `preview`, `apply` and `guarded_backends` for this
fixed recipe. A trusted operator provisions a transient systemd service with CPU quota 100000/100000,
1 GiB memory, no swap and at most 128 processes. `resource_guard.capture` checks actual cgroup v2
membership, kernel limits, boot and cgroup identities. The reviewed lifecycle checks those facts
again before worker and namespace release. This does not provide coding-agent or model-service access.

The approved native executor policy must contain the exact `guard.descriptor` in `resource_guard`.
That closed optional descriptor participates in the native policy, contract and authorization digest
and is retained in native history. Direct native apply enforces the declared guard; changing to a
broader resource envelope cannot reuse an earlier grant. Historical replay preserves its original
resource evidence. Existing policies without the field keep their contract and make no CPU/memory/
process-containment claim.

`resource_guard.run_unit` provisions the reviewed bounded service as the current non-root user.
It accepts only trusted argv and explicit environment variable names; forwarded values stay out of
argv and logs. The whole unit is stopped on timeout/output overflow. Secrets and canonical journals
stay outside candidate mounts. The same unit can retain journals while a controller child restarts;
another runner, boot, cgroup or replacement journal cannot resume that contract.

Mandatory `test_worker_integration_runtime.py` cases self-provision actual transient services on
supported Ubuntu. They verify fixed-recipe execution/replay, unavailable setup, cancellation,
controller-process recovery in the same unit, and actual CPU throttling, process denial and memory
OOM behavior. Missing systemd, sudo, cgroup enforcement or Bubblewrap fails full checks; the optional
development subset excludes the real runtime modules and establishes no runtime verification.
Lifecycle activation records this implemented fixed-command contract. Fresh supported-runtime CI
observations establish verification; controlled preparation tests alone cannot do so.

## Exact output association

`exacly.output_association` provides a separately initialized journal and explicit `apply`/`recover`
invocations for associating one native published handoff with its original registered goal. Trusted
configuration pins the journal identities, original binding, state, model, plan and policies.
It checks retained handoff provenance, rereads current state and the active native binding under
ordered locks, and retains the exact native replacement registration before publishing it.

The replacement preserves the original pending goal and planning input while assigning the exact
handoff output revision. It does not write observed state, admit evidence or complete work. Recovery
reconciles the exact retained native record; drift conflicts instead of rebuilding a different
registration. Intent, receipt and successful replay synchronize the pinned journal directory.

The 25 controlled integration/fault cases include duplicate processes, exits on both sides of the
native binding write, divergent state and failed directory synchronization. They establish this
association primitive, not the complete REQ-038 fresh-evidence and native-completion workflow.

## Durable remote publication protocol

`exacly.remote_publication` retains exact native published handoff provenance and separately scoped
remote intent in an initialized private journal. Trusted configuration selects a `RemoteTransport`
implementation and pins its identity, immutable remote repository ID, destination, expected old
revision, exact handoff commit/tree/receipt and native journal identities. The one-shot authorization
excludes base-history import: the remote base must already exist, and only the retained handoff delta
may be supplied. Native publication, evidence, merge, release and deployment remain separate effects.

The journal synchronizes prepared and sent intent before a single checked transport request.
After an uncertain send, recovery only discovers. It cannot resend because discovery is empty or the
old ref remains. Publication requires exact objects, destination and independently established stable
operation correlation. Directory synchronization also gates historical replay after a prior error.
The 39 controlled protocol/fault tests include process loss, duplicate processes, drift and failed
intent/sent/receipt synchronization; no remote network is used.

The concrete `git_remote_transport` now sends explicit atomic expected-old receive-pack commands
and a separately approved authenticated operation marker. Its protected key, exact repository
identity and producer selection contracts are described in [remote-publication.md](remote-publication.md).
Real local Git server tests establish transport behavior; opt-in live acceptance remains required.
REQ-037 remains proposed until that live coverage is retained.

## Native workflow controller

`automation_controller` coordinates the exact native adapters and reobserves after native completion.
`controller_store` retains phase intent, correlations and initialized ledger anchors separately from
component journals. `controller_runtime` supplies an independent gated deadline supervisor and
descendant cleanup. Protected policy/configuration digests, exact stores and authoritative objective
accounting remain prerequisites for every effect. The closed initial profile has one required
producer on one local coordination host; unsupported aggregate mappings and distributed ownership
are rejected. See [unattended-controller.md](unattended-controller.md).

The fixed first-pilot consumer, branch, verifier, worker paths, App credential requirements, bounded
same-runner coordinator and independent approval procedure are in
[unattended-pilot-approval.md](unattended-pilot-approval.md). A generated packet or copied policy does
not approve itself. Runner loss cannot be repaired by recreating this original goal's stores.

## Controlled foundation integration

`python/tests/test_unattended_foundations_integration.py` connects the fixed snapshot-inventory
recipe to real native isolated execution, exact local Git handoff, the controlled remote protocol,
current-state association and native completion. Explicit trusted fixture decisions reserve costs
before effects. Wrong-revision evidence keeps work pending; a distinct fresh controlled producer run
is necessary for completion. Replays preserve unrelated state and the original objective's budget.

The test requires the supported Bubblewrap runtime and must fail if that boundary is unavailable.
Its remote and producer transports are controlled test doubles. Its explicit fixture decisions
do not substitute for standing-policy evaluation, whole-workflow or live GitHub acceptance. Run it in the
existing full supported-runtime CI; local collection/setup checks alone establish no execution result.

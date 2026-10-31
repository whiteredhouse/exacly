# Unattended verification foundations

The full unattended verification contracts, REQ-034–039, remain proposed in
[`spec/exacly/unattended-automation.yaml`](../spec/exacly/unattended-automation.yaml).
The implementation checkpoint is [`automation-progress.md`](automation-progress.md).
These foundations do not enable an unattended workflow by themselves.

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
an effect happened, and the future controller must reconcile them before any retry.

This first slice has no standing-policy evaluator, revocation service, native authorization adapter
or controller. Its tests establish local accounting semantics only. They do not establish REQ-035
in full, independent policy approval, a worker boundary, remote publication or live completion.

The Python API exposes `initialize`, `enroll`, `reserve`, `inspect`, `mark_uncertain` and `settle`.
`reserve(..., approved=True, reason=...)` records a trusted caller's decision; the boolean is not
an approval mechanism and this API must never be exposed to candidate callers. Costs and limits
use exactly `attempts`, `wall_time_ms`, `model_requests` and `model_spend_microunits`, each a finite
nonnegative integer. Exact request replay returns its current retained status without recharging.

There is no supported SQLite hot-journal recovery or store migration API in this foundation.
If an interrupted transaction leaves a sidecar, preserve the database and sidecar together and
escalate. Normal APIs refuse them before SQLite can recover implicitly. Never delete the sidecar
or initialize a replacement store to resume with fresh capacity.
